#!/bin/bash
# 把危險感知影片推上 Cloudflare R2。一次性任務 —— 不要為它掛 launchd。
#
# 為什麼是 R2 而不是從這台供應：333 MB、每個訪客都要拉，走的是家用上行。
# R2 的 egress 免費且無上限，0.33 GB 對免費層的 10 GB-month 是 3%，實付 $0。
#
# 憑證：~/.secrets/r2.env（chmod 600，Syncthing 兩機同步）。不進 repo。
#   R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET
# 那組 key 由 dashboard 的 R2 → Manage R2 API Tokens 產生，跟 ~/.secrets/cloudflare_token
# 完全是兩回事（後者只有 Tunnel+DNS 權限，打 R2 API 一律回 code 10000）。
#
# 驗收標準寫死在下面：檔數與總量對不上就 exit 1。
# 「指令沒報錯」不算成功 —— aws s3 sync 少傳幾個檔案是會靜靜地成功的。
set -euo pipefail

cd "$(dirname "$0")/.."

# ── 預期狀態（改了轉檔參數就要一起改，對不上會擋下來）─────────────
EXPECT_MP4=119
EXPECT_JPG=119
EXPECT_FILES=238
EXPECT_BYTES=332728436          # 323,255,497 mp4 + 9,472,939 jpg
TOLERANCE_BYTES=0               # 內容是決定性的，不該有誤差

SRC="data/videos"
SECRETS="${DL_R2_SECRETS:-$HOME/.secrets/r2.env}"

# ── 憑證 ───────────────────────────────────────────────────────
if [[ ! -f "$SECRETS" ]]; then
  echo "找不到 $SECRETS" >&2
  echo "先在 dashboard 啟用 R2 並產一組 Object Read & Write 的 API token，然後：" >&2
  echo "  install -m 600 /dev/null ~/.secrets/r2.env && cat > ~/.secrets/r2.env" >&2
  exit 78
fi
# 比照 deploy/run-server.sh：先驗格式再 source。Syncthing 的 .sync-conflict
# 污染檔不會是 KEY=VALUE，先掃一遍就攔得住（set -e 之下 source 失敗會直接殺掉
# 腳本，訊息永遠印不出來）。
if grep -qvE '^[[:space:]]*(#|[A-Za-z_][A-Za-z0-9_]*=|$)' "$SECRETS"; then
  echo "$SECRETS 含有不是 KEY=VALUE 的行，拒絕以未知組態執行" >&2
  exit 78
fi
set -a; source "$SECRETS"; set +a

for v in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET; do
  [[ -n "${!v:-}" ]] || { echo "$SECRETS 少了 $v" >&2; exit 78; }
done

ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
# 明確覆寫 profile 與 region：這台的 ~/.aws 指向 AWS 的憑證，不隔開的話
# 會拿 AWS 的 key 去打 R2（症狀是 403，但原因看不出來）。R2 只認 region=auto。
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION=auto
# unset 而不是設成空字串：AWS_PROFILE="" 會被當成「名為空字串的 profile」，
# 錯誤訊息是 "The config profile () could not be found"（實測過）。
unset AWS_PROFILE AWS_DEFAULT_PROFILE
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required   # R2 不吃新版 CRC32 預設值
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
aws() { command aws --endpoint-url "$ENDPOINT" "$@"; }

# ── 本機先自檢，錯的東西不要上雲 ─────────────────────────────────
n_mp4=$(find "$SRC" -name '*.mp4' | wc -l | tr -d ' ')
n_jpg=$(find "$SRC" -name '*.jpg' | wc -l | tr -d ' ')
bytes=$(find "$SRC" -type f \( -name '*.mp4' -o -name '*.jpg' \) -exec stat -f%z {} + | paste -sd+ - | bc)

echo "本機：${n_mp4} mp4 + ${n_jpg} jpg = $((n_mp4 + n_jpg)) 個檔案，${bytes} bytes"
if [[ "$n_mp4" != "$EXPECT_MP4" || "$n_jpg" != "$EXPECT_JPG" || "$bytes" != "$EXPECT_BYTES" ]]; then
  echo "與預期不符（預期 ${EXPECT_MP4}+${EXPECT_JPG}=${EXPECT_FILES}、${EXPECT_BYTES} bytes）" >&2
  echo "先跑 tools/fetch_hazard_videos.py 與 tools/transcode_hazard_videos.py" >&2
  exit 1
fi

# ── bucket ────────────────────────────────────────────────────
if aws s3api head-bucket --bucket "$R2_BUCKET" >/dev/null 2>&1; then
  echo "bucket $R2_BUCKET 已存在"
else
  echo "建立 bucket $R2_BUCKET"
  aws s3api create-bucket --bucket "$R2_BUCKET" >/dev/null
fi

# ── 上傳 ──────────────────────────────────────────────────────
# content-type 一定要明講：aws cli 靠副檔名猜，猜錯的話瀏覽器不會播（會下載）。
# immutable 是誠實的：檔名是「原始檔 md5 ＋ 轉檔參數」的雜湊，內容變檔名一定變。
CACHE="public, max-age=31536000, immutable"
echo "上傳 mp4…"
aws s3 sync "$SRC" "s3://$R2_BUCKET/videos/" \
  --exclude '*' --include '*.mp4' \
  --content-type video/mp4 --cache-control "$CACHE" --only-show-errors
echo "上傳 jpg…"
aws s3 sync "$SRC" "s3://$R2_BUCKET/videos/" \
  --exclude '*' --include '*.jpg' \
  --content-type image/jpeg --cache-control "$CACHE" --only-show-errors

# ── 對帳：用 R2 實際列出來的東西，不是用 exit code ────────────────
echo
echo "向 R2 對帳…"
listing=$(aws s3api list-objects-v2 --bucket "$R2_BUCKET" --prefix videos/ \
          --query 'Contents[].[Key,Size]' --output text)
r_mp4=$(grep -c '\.mp4' <<<"$listing" || true)
r_jpg=$(grep -c '\.jpg' <<<"$listing" || true)
r_bytes=$(awk '{s+=$2} END{print s+0}' <<<"$listing")
r_files=$(wc -l <<<"$listing" | tr -d ' ')

echo "R2：${r_mp4} mp4 + ${r_jpg} jpg = ${r_files} 個物件，${r_bytes} bytes"
ok=1
[[ "$r_mp4"   == "$EXPECT_MP4"   ]] || { echo "  ✗ mp4 數不符" >&2; ok=0; }
[[ "$r_jpg"   == "$EXPECT_JPG"   ]] || { echo "  ✗ jpg 數不符" >&2; ok=0; }
[[ "$r_files" == "$EXPECT_FILES" ]] || { echo "  ✗ 物件數不符" >&2; ok=0; }
[[ "$r_bytes" == "$EXPECT_BYTES" ]] || { echo "  ✗ 總量不符（差 $((r_bytes - EXPECT_BYTES)) bytes）" >&2; ok=0; }

# 抽驗一個物件的標頭：sync 會跳過「已存在」的檔案，而它比對的是大小與時間，
# 不是 metadata —— 標頭設錯的舊物件重跑不會被修正，只有實際去看才知道。
probe=$(find "$SRC" -name '*.mp4' | head -1 | xargs basename)
meta=$(aws s3api head-object --bucket "$R2_BUCKET" --key "videos/$probe" \
       --query '[ContentType,CacheControl]' --output text)
echo "抽驗 videos/$probe → $meta"
[[ "$meta" == "video/mp4"*"immutable"* ]] || { echo "  ✗ 標頭不對" >&2; ok=0; }

[[ "$ok" == 1 ]] || { echo "對帳失敗" >&2; exit 1; }

echo
echo "✓ 對帳通過：${EXPECT_FILES} 個物件、${EXPECT_BYTES} bytes"
echo
echo "接下來："
echo "  1. dashboard → R2 → $R2_BUCKET → Settings → Connect Domain → videos.wordnexus.lol"
echo "  2. 在 ~/.secrets/driver-license.env 加一行："
echo "       DL_VIDEO_ORIGIN=https://videos.wordnexus.lol"
echo "  3. 依序：bash ops/backup.sh → git pull && uv sync →"
echo "     launchctl kickstart -k gui/\$(id -u)/com.driverlicense.server"
echo "  4. 用瀏覽器（不是 curl）驗 CSP 與影片真的載得起來"
