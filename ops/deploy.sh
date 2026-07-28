#!/usr/bin/env bash
# felix 上的換版流程。
#
# 為什麼要有這支：README 的五個步驟全靠人照著手打，而其中一個順序約束是踩過
# 才知道的 —— ops/backup.sh 的健檢閘跟著 schema 走，先 git pull 的話新版腳本
# 會對上還沒遷移的舊 DB，直接 exit 5。把它寫成程式碼，順序就不會再錯一次。
#
# 用法（在 felix 上，repo 根目錄）：
#   bash ops/deploy.sh
#
# 依序做這些事，任何一步失敗就停下來並說清楚下一步該做什麼：
#   1. 前置檢查：launchd 服務在不在、工作區乾不乾淨
#   2. 備份（必須在 pull 之前）
#   3. git pull && uv sync
#   4. pytest —— 不過就停在這裡，服務還沒重啟，跑的仍是舊程式碼
#   5. launchctl kickstart 重啟
#   6. 打 /api/health：程序活著、DB 開得起來、schema 遷到程式碼期待的版本
#
# ⚠️ WorkingDirectory 就是這個 repo，KeepAlive=true。第 3 步之後、第 5 步之前
# 這段期間，只要程序因為任何原因重啟（崩潰、開機），它就會載入剛 pull 下來的
# 程式碼並跑 db.init()。這個窗口沒辦法消除，只能讓它短 —— 所以測試放在 pull
# 之後、重啟之前，而不是更早。
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${DL_PORT:-8006}"
SERVICE="gui/$(id -u)/com.driverlicense.server"

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 1. 前置檢查 ─────────────────────────────────────────────────────────
say "前置檢查"
launchctl print "$SERVICE" >/dev/null 2>&1 \
  || die "找不到 launchd 服務 $SERVICE。這支腳本只在 felix（常駐機）上跑。"

# 服務的 WorkingDirectory 就是它會 import 的那份程式碼。在別的目錄跑這支腳本
# （最容易發生的是 git worktree：路徑長得很像、git 指令全都能動）會 pull 錯的
# 分支、跑錯的測試，然後 kickstart 重啟的仍然是原本那份 —— 每一步都成功，
# 而實際上什麼都沒換版。這個檢查就是在擋那個「看起來部署成功」的情況。
svc_dir="$(launchctl print "$SERVICE" 2>/dev/null \
  | sed -n 's/^[[:space:]]*working directory = //p' | head -1)"
here="$(pwd -P)"
[[ -n "$svc_dir" ]] || die "讀不到服務的 WorkingDirectory，無法確認部署目標。"
[[ "$svc_dir" == "$here" ]] || die "服務的 WorkingDirectory 是
       $svc_dir
     但這支腳本跑在
       $here
     在這裡部署不會換到生產環境的程式碼。先 cd 到上面那個目錄。"

if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  die "工作區不乾淨。這個 repo 就是生產環境的工作目錄，未提交的改動會被
     git pull 混進去，而且下一次重啟就會被載入。先處理掉再部署。"
fi
echo "  服務存在、工作區乾淨"

# ── 2. 備份（必須在 pull 之前）───────────────────────────────────────────
say "備份進度 DB"
bash ops/backup.sh || die "備份失敗（看 ~/Library/Logs/driver-license-backup.log）。
     沒有備份就不要往下走 —— 下一步可能觸發不可逆的 schema 遷移。"

# ── 3. 取得新版 ─────────────────────────────────────────────────────────
say "git pull && uv sync"
before="$(git rev-parse HEAD)"
git pull --ff-only
uv sync
after="$(git rev-parse HEAD)"
if [[ "$before" == "$after" ]]; then
  echo "  已經是最新的（$after）"
else
  echo "  $before → $after"
fi

# ── 4. 測試 ─────────────────────────────────────────────────────────────
say "跑測試"
uv run pytest -q || die "測試不過，服務沒有重啟，跑的仍是舊程式碼。
     修好再跑一次，或 git reset --hard $before 退回去。"

# ── 5. 重啟 ─────────────────────────────────────────────────────────────
say "重啟服務"
launchctl kickstart -k "$SERVICE"

# ── 6. 健康檢查 ─────────────────────────────────────────────────────────
say "健康檢查"
# 期待的 schema 版本直接讀剛 checkout 出來的程式碼，不是寫死在這裡 ——
# 寫死的話，改了 SCHEMA_VERSION 卻忘了改這支，這一步就會變成永遠通過。
expected="$(sed -n 's/^SCHEMA_VERSION = \([0-9]*\).*/\1/p' app/db.py)"
[[ -n "$expected" ]] || die "讀不到 app/db.py 的 SCHEMA_VERSION"

health=""
for i in $(seq 1 20); do
  if health="$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)"; then
    break
  fi
  sleep 0.5
done
[[ -n "$health" ]] || die "10 秒內連不上 http://127.0.0.1:${PORT}/api/health。
     看 ~/Library/Logs/driver-license.err.log —— KeepAlive=true 會把崩潰迴圈
     藏起來（每 10 秒重啟一次、對外 502），不看 log 是看不出來的。"

got="$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["schema_version"])')"
[[ "$got" == "$expected" ]] || die "schema 版本是 $got，程式碼期待 $expected。
     遷移沒跑完或跑錯了。DB 在 \$DL_DATA_DIR，備份在 \$DL_DATA_DIR/backups/。"

printf '%s\n' "$health"
say "部署完成（schema v${got}）"
