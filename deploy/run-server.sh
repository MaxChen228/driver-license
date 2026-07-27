#!/bin/bash
# launchd 的進入點。存在的理由：憑證不能進 repo，而 plist 是進 repo 的。
#
# ~/.secrets/driver-license.env（chmod 600，Syncthing 兩機同步）內容像這樣：
#   DL_ENV=prod
#   DL_TURNSTILE_SITEKEY=...
#   DL_TURNSTILE_SECRET=...
# 沒有這個檔也能跑（開發模式：/api/docs 開著、Turnstile 關閉），
# 但正式站少了 DL_ENV=prod 會把 API 文件攤在公網上，所以下面會明確 log 出目前模式。
#
# 沒有 session 簽章金鑰之類的東西要保管：session token 是 256 bit 隨機值，
# DB 只存它的 SHA-256，不需要對稱金鑰。少一個要輪替的秘密。
set -euo pipefail

cd "$(dirname "$0")/.."

SECRETS="${DL_SECRETS_FILE:-$HOME/.secrets/driver-license.env}"
if [[ -f "$SECRETS" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$SECRETS"; set +a
else
  echo "run-server: 找不到 $SECRETS，以開發模式啟動（/api/docs 開啟、Turnstile 關閉）" >&2
fi

exec .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8006 \
  --proxy-headers --forwarded-allow-ips 127.0.0.1
