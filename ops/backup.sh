#!/usr/bin/env bash
# driver-license 進度 DB 每日備份 → 本地輪替 + 冷磁碟（DL_BACKUP_DEST_DIR）或 AWS S3。
#
# 為什麼一定要用 sqlite3 .backup 而不是 cp／tar：
#   這個 DB 開 WAL 模式，主檔可以長期停在 4KB 而所有已提交的資料都還在 app.db-wal 裡
#   （實測 2026-07-27：app.db 4096 bytes、app.db-wal 197792 bytes）。直接複製主檔會拿到
#   一個「語法正確但幾乎空白」的 DB，而且完全不會報錯 —— 是最惡劣的那種備份失敗。
#   .backup 走 SQLite 的線上備份 API，會把 WAL 的內容一起收斂進快照，且不必停服務。
#   （kg_backup.sh 排除 *-wal/*-shm 是對的，因為它備份整個目錄、主檔本身是完整的；
#     這裡只備份單一 DB 檔，語義不同，不能照抄。）
#
# 產出：
#   本地 $DATA_DIR/backups/app-YYYY-MM-DD.db.gz  （保留 $KEEP 份，就地快速還原用）
#   冷磁碟 $DL_BACKUP_DEST_DIR/driver-license/YYYY-MM-DD.db.gz （2026-10-23 起；保留 $COLD_KEEP 份）
#         目錄必須已存在（磁碟沒掛載 → exit=6，不會寫進內建碟）。
#   S3   s3://$BUCKET/driver-license/YYYY-MM-DD.db.gz （DL_BACKUP_DEST_DIR 未設時的舊路徑）
#
# 每次執行寫一行稽核 log：<timestamp> exit=<rc> bytes=<size> sha256=<hash> key=<s3 key>
#
# 還原：
#   gunzip -c app-2026-07-27.db.gz > ~/driver-license-data/app.db
#   rm -f ~/driver-license-data/app.db-wal ~/driver-license-data/app.db-shm
#   launchctl kickstart -k gui/$(id -u)/com.driverlicense.server
set -euo pipefail

DATA_DIR="${DL_DATA_DIR:-$HOME/driver-license-data}"
BUCKET="${DL_BACKUP_BUCKET:-butler-backups-967512079054}"
REGION="${DL_BACKUP_REGION:-ap-northeast-1}"
LOG="${DL_BACKUP_LOG:-$HOME/Library/Logs/driver-license-backup.log}"
KEEP="${DL_BACKUP_KEEP:-14}"
COLD_DIR="${DL_BACKUP_DEST_DIR:-}"
COLD_KEEP="${DL_BACKUP_COLD_KEEP:-90}"

DB="$DATA_DIR/app.db"
OUT_DIR="$DATA_DIR/backups"
DATE="$(date -u +%Y-%m-%d)"
DEST="$OUT_DIR/app-${DATE}.db.gz"
KEY="driver-license/${DATE}.db.gz"

mkdir -p "$OUT_DIR" "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }
trap 'rc=$?; [[ $rc -eq 0 ]] || log "exit=$rc (unexpected)"; exit $rc' ERR

if [[ ! -f "$DB" ]]; then
  log "exit=2 missing db: $DB"
  exit 2
fi

TMP="$(mktemp -t dl-backup)"
trap 'rm -f "$TMP" "$TMP.gz"' EXIT

# .backup 到暫存檔再壓縮。不串流是刻意的：要在上傳前先驗證這份快照真的開得起來。
sqlite3 "$DB" ".backup '$TMP'"

# 完整性驗證 —— 備份最常見的失敗是「檔案有了但內容是壞的」。沒驗過就不算備份。
INTEGRITY="$(sqlite3 "$TMP" 'PRAGMA integrity_check;' | head -1)"
if [[ "$INTEGRITY" != "ok" ]]; then
  log "exit=3 integrity_check failed: $INTEGRITY"
  exit 3
fi
# 健全性閘：至少要有一個帳號。不能用 card_state 的筆數 —— 惰性建列之後，
# 一個剛註冊、還沒答過任何一題的帳號合法地是 0 列，拿它當閘會誤殺真備份。
# 帳號表則是「DB 有東西」的最低保證：0 個帳號代表拿到的是空殼或壞檔。
USERS="$(sqlite3 "$TMP" 'SELECT COUNT(*) FROM user;' 2>&1 || true)"
CARDS="$(sqlite3 "$TMP" 'SELECT COUNT(*) FROM card_state;' 2>/dev/null || echo '?')"
EVENTS="$(sqlite3 "$TMP" 'SELECT COUNT(*) FROM review_event;' 2>/dev/null || echo '?')"
# 先驗「拿到的是一個數字」再驗「數字不是 0」。這兩件事的死因完全不同：
# 非數字代表 schema 不對（多半是 git pull 換了這個腳本、但服務還沒 kickstart
# 遷移，DB 仍是 v1 沒有 user 表），0 代表快照是空殼。
# 不先分開的話，bash 算術語境會把空字串當 0，於是 schema 不符會被誤報成「零帳號」。
if ! [[ "$USERS" =~ ^[0-9]+$ ]]; then
  log "exit=5 cannot read user count (schema mismatch? not migrated yet): $USERS"
  exit 5
fi
if [[ "$USERS" -eq 0 ]]; then
  log "exit=4 snapshot has zero users — refusing to overwrite a good backup with an empty one"
  exit 4
fi

gzip -9 -c "$TMP" >"$TMP.gz"
mv "$TMP.gz" "$DEST"
SIZE="$(wc -c <"$DEST" | tr -d ' ')"
SHA="$(shasum -a 256 "$DEST" | awk '{print $1}')"

# 本地輪替：只留最近 $KEEP 份
ls -1t "$OUT_DIR"/app-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done

rc=0
if [[ -n "$COLD_DIR" ]]; then
  if [[ -d "$COLD_DIR" && -w "$COLD_DIR" ]] && mkdir -p "$COLD_DIR/driver-license"; then
    cp "$DEST" "$COLD_DIR/${KEY}.part" && mv -f "$COLD_DIR/${KEY}.part" "$COLD_DIR/${KEY}" || rc=$?
    if [[ $rc -eq 0 ]]; then
      ls -1 "$COLD_DIR/driver-license" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.db\.gz$' | sort -r \
        | tail -n "+$((COLD_KEEP + 1))" | while read -r old; do rm -f "$COLD_DIR/driver-license/$old"; done
    else
      rm -f "$COLD_DIR/${KEY}.part"
    fi
  else
    rc=6
  fi
elif command -v aws >/dev/null 2>&1; then
  aws s3 cp "$DEST" "s3://${BUCKET}/${KEY}" --region "$REGION" --no-progress >/dev/null || rc=$?
else
  rc=5
fi

log "exit=$rc bytes=$SIZE sha256=$SHA users=$USERS cards=$CARDS events=$EVENTS key=$KEY"
exit "$rc"
