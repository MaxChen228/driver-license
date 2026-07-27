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
  # 先驗格式再 source。原本寫成 `if ! { set -a; source ...; set +a; }` 是無效的
  # 保護：set -e 會在檔案裡出錯的那一行就把整個腳本殺掉（訊息永遠印不出來），
  # 而就算沒被殺，那個群組的結束狀態取自最後一個指令 `set +a`，恆為 0。
  # 真正要擋的情境是 Syncthing 留下的 .sync-conflict 之類的污染檔 —— 那種東西
  # 不會是 KEY=VALUE，先掃一遍就攔得住。而 plist 是 KeepAlive=true，
  # 沒攔住的話症狀是無聲的重啟迴圈。
  if grep -qvE '^[[:space:]]*(#|[A-Za-z_][A-Za-z0-9_]*=|$)' "$SECRETS"; then
    echo "run-server: $SECRETS 含有不是 KEY=VALUE 的行，拒絕以未知組態啟動" >&2
    grep -nvE '^[[:space:]]*(#|[A-Za-z_][A-Za-z0-9_]*=|$)' "$SECRETS" | head -3 >&2
    exit 78
  fi
  # shellcheck disable=SC1090
  set -a; source "$SECRETS"; set +a
else
  echo "run-server: 找不到 $SECRETS，以開發模式啟動（/api/docs 開啟、Turnstile 關閉）" >&2
fi

# 打錯成 production / Production 會靜默退回開發模式，把 /openapi.json 攤在公網上。
case "${DL_ENV:-dev}" in
  prod|dev) ;;
  *) echo "run-server: DL_ENV=\"$DL_ENV\" 不是 prod 或 dev，拒絕啟動" >&2; exit 78 ;;
esac

# 刻意「不」開 --proxy-headers。uvicorn 的 ProxyHeadersMiddleware 會直接把
# scope["client"] 改寫成 X-Forwarded-For 的值，於是 ratelimit.client_ip 那道
# 「peer 是 loopback 才信任代理標頭」的閘永遠不成立 —— 實測帶
# X-Forwarded-For: 9.9.9.9 時 request.client.host 就變成 9.9.9.9。
# 保持 peer 是真實的 127.0.0.1（cloudflared 在本機），代理標頭的信任由我們自己判。
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8006
