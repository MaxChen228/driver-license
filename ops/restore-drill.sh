#!/usr/bin/env bash
# 還原演練。
#
# ops/backup.sh 已經驗過快照本身（integrity_check + 至少一個帳號），但「還原
# 步驟寫在註解裡」與「還原真的可行」是兩件事。這支把那段註解變成會失敗的程式碼：
# 真的解壓一份備份、真的用它起一個實例、真的打 /api/health 看它活著。
#
# 全程不碰生產：DL_DATA_DIR 指到臨時目錄，服務起在另一個埠（預設 8007）。
# 生產的 launchd 服務完全不受影響，不需要停機。
#
# 用法：
#   bash ops/restore-drill.sh            # 用最新的本地備份
#   bash ops/restore-drill.sh --s3       # 從 S3 抓最新那份（驗異地副本真的能用）
#
# 建議每月跑一次。備份最貴的失敗模式是「一直在跑、也一直是綠的，直到要用的
# 那天才發現還原不回來」——那時候已經沒有第二次機會了。
set -euo pipefail

cd "$(dirname "$0")/.."

DATA_DIR="${DL_DATA_DIR:-$HOME/driver-license-data}"
BUCKET="${DL_BACKUP_BUCKET:-butler-backups-967512079054}"
REGION="${DL_BACKUP_REGION:-ap-northeast-1}"
LOG="${DL_DRILL_LOG:-$HOME/Library/Logs/driver-license-drill.log}"
PORT="${DL_DRILL_PORT:-8007}"
SOURCE="local"
[[ "${1:-}" == "--s3" ]] && SOURCE="s3"

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
log() { mkdir -p "$(dirname "$LOG")"; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; log "FAIL $*"; exit 1; }

WORK="$(mktemp -d -t dl-drill)"
SERVER_PID=""
cleanup() {
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── 取得一份備份 ────────────────────────────────────────────────────────
say "取得備份（$SOURCE）"
if [[ "$SOURCE" == "s3" ]]; then
  command -v aws >/dev/null 2>&1 || die "沒有 aws CLI，跑不了 --s3"
  key="$(aws s3 ls "s3://${BUCKET}/driver-license/" --region "$REGION" \
        | sort | tail -1 | awk '{print $4}')"
  [[ -n "$key" ]] || die "S3 上找不到任何備份（s3://${BUCKET}/driver-license/）"
  aws s3 cp "s3://${BUCKET}/driver-license/${key}" "$WORK/backup.db.gz" \
    --region "$REGION" --no-progress >/dev/null || die "S3 下載失敗"
  origin="s3://${BUCKET}/driver-license/${key}"
else
  newest="$(ls -1t "$DATA_DIR"/backups/app-*.db.gz 2>/dev/null | head -1 || true)"
  [[ -n "$newest" ]] || die "$DATA_DIR/backups/ 裡沒有備份。先跑一次 ops/backup.sh。"
  cp "$newest" "$WORK/backup.db.gz"
  origin="$newest"
fi
echo "  來源：$origin"

# ── 解壓與靜態檢查 ──────────────────────────────────────────────────────
say "解壓並檢查"
mkdir -p "$WORK/data"
gunzip -c "$WORK/backup.db.gz" >"$WORK/data/app.db" || die "解壓失敗（檔案損壞？）"

integrity="$(sqlite3 "$WORK/data/app.db" 'PRAGMA integrity_check;' | head -1)"
[[ "$integrity" == "ok" ]] || die "integrity_check 失敗：$integrity"

users="$(sqlite3 "$WORK/data/app.db" 'SELECT COUNT(*) FROM user;')"
events="$(sqlite3 "$WORK/data/app.db" 'SELECT COUNT(*) FROM review_event;')"
states="$(sqlite3 "$WORK/data/app.db" 'SELECT COUNT(*) FROM card_state;')"
before_ver="$(sqlite3 "$WORK/data/app.db" 'PRAGMA user_version;')"
echo "  帳號 $users · 事件 $events · 卡片狀態 $states · schema v$before_ver"
[[ "$users" -gt 0 ]] || die "備份裡一個帳號都沒有 —— 這不是一份有內容的快照"

# ── 真的起一個實例 ──────────────────────────────────────────────────────
# 這一步才是重點。前面全部通過但服務起不來（例如 schema 版本比程式新、
# 遷移路徑不存在）的情況，只有真的 import 一次才會現形。
say "用這份備份起一個實例（:$PORT）"
DL_DATA_DIR="$WORK/data" DL_ENV=dev .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$PORT" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

health=""
for _ in $(seq 1 30); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    tail -20 "$WORK/server.log" >&2
    die "服務起不來（上面是它的輸出）。這正是這支腳本存在的理由。"
  fi
  if health="$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)"; then
    break
  fi
  sleep 0.5
done
[[ -n "$health" ]] || { tail -20 "$WORK/server.log" >&2; die "15 秒內 /api/health 沒有回應"; }

expected="$(sed -n 's/^SCHEMA_VERSION = \([0-9]*\).*/\1/p' app/db.py)"
got="$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["schema_version"])')"
[[ "$got" == "$expected" ]] || die "還原後 schema 是 v$got，程式碼期待 v$expected"

# ── 還原前後的資料量要對得上 ────────────────────────────────────────────
# 遷移可以改 schema，但不能把資料弄不見。v1→v2 那種重建式遷移尤其要盯 ——
# 它是 INSERT…SELECT 搬表，搬漏了不會有任何錯誤。
say "對帳"
after_users="$(sqlite3 "$WORK/data/app.db" 'SELECT COUNT(*) FROM user;')"
after_events="$(sqlite3 "$WORK/data/app.db" 'SELECT COUNT(*) FROM review_event;')"
[[ "$after_users" == "$users" ]] || die "帳號數從 $users 變成 $after_users"
# 事件數只在 v1→v2 的重建遷移中可能改變（那次是全搬），其餘一律相等
[[ "$after_events" == "$events" ]] || die "事件數從 $events 變成 $after_events"
echo "  帳號 $after_users · 事件 $after_events · schema v$got —— 與還原前一致"

kill "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""

log "OK source=$origin users=$users events=$events states=$states schema=$got"
say "還原演練通過（來源 $origin）"
