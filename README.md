# driver-license

台灣汽機車駕照筆試刷題 app。題庫從監理所公告 PDF 抽出，複習排程移植自 `~/project/kg` 的 flash card system。

部署在 felix 常駐機，對外兩條路：

| 入口 | 網址 | 誰看得到 |
|---|---|---|
| Cloudflare Tunnel | https://drive.wordnexus.lol | 公網 |
| tailscale serve | https://s-macbook-air.tail8c391d.ts.net | tailnet |

兩條都終結在 `127.0.0.1:8006`。**這推翻了本專案早期「tailnet 即邊界、無認證、
絕不掛 Cloudflare Tunnel」的決定** —— 邊界改由 app 自己守（見「身分模型」）。

## 題庫

| deck | 來源 | 題數 | 圖 |
|---|---|---|---|
| `car` | 新版汽車筆試題庫 115.5.29 | 1090 | 206 |
| `moto` | 機車駕照筆試題庫 115.1 | 806 | 148 |

抽取結果經**字元級對帳**驗證：重複字元 0，未進題目文字的字元可逐項解釋完（題號／答案欄／表頭／章節標題／頁碼）。汽車的三架構題數 424 / 230 / 436 與 PDF 自印的合計行完全吻合 —— 這是不依賴本專案程式碼的獨立佐證。

```bash
uv run python tools/verify.py     # 題庫稽核，全綠才算數
```

題庫改版時重跑 `tools/extract_{car,moto}.py` → `assemble.py` → `export_images.py`，再跑 verify。
**題目內容留在 JSON、不進 DB**，所以覆蓋題庫不需要任何 migration。

### 危險感知影片題

| deck | 來源 | 題數 | 影片 |
|---|---|---|---|
| `hazard_car` | 1150605-危險感知影片題(調整影片順序).pdf | 10 | 10 |
| `hazard_moto` | (中文)機車危險感知影片選擇題.pdf | 126 | 109 |

每題綁一支情境影片。題目 schema 與筆試完全相同，只多 `video`（轉檔後的檔名）與
`video_no`（公路局的影片編號）—— 所以 SRS、佇列、瀏覽、統計、離線快取整條路都不必改。

126 題只對到 109 支影片：**有 17 題與另一題共用同一段情境**。輸出以「原始檔 md5 ＋
轉檔參數」的雜湊命名，共用的題目因此指到同一個檔案（省 12% 流量，而且做完這題再做
下一題時瀏覽器直接命中快取）。參數進雜湊是必要的：只用 md5 的話日後調 CRF 檔名不變，
而這些檔案掛的是 immutable。

**影片不進 git，也刻意不留離線副本** —— 來源是公路局公開檔案、轉檔可重現，
這是快取不是資料。重建路徑（約 40 分鐘，其中下載 ~15 分、轉檔 ~25 分）：

```bash
uv run python tools/extract_hazard.py          # PDF → data/source/hazard_*.json（136 題）
uv run python tools/fetch_hazard_videos.py     # → data/source/videos/  136 支 0.70 GB
uv run python tools/transcode_hazard_videos.py # → data/videos/  238 個檔案 332,728,436 bytes
uv run python tools/build_hazard_decks.py      # → data/hazard_*_questions.json
```

三支都可以中斷後重跑（已完成的會跳過）。預期產出：**119 mp4（323,255,497 bytes）
＋ 119 jpg poster（9,472,939 bytes）**，`ops/upload_videos.sh` 會拿這組數字對帳。

下載不需要瀏覽器：GET 分享頁時伺服器就在 `Set-Cookie` 給一枚
`drive-sharing-<sharing_link>`，那正是 download API 要的 `sharing_token`；
少了它一律回 `{"error":{"code":1002}}`。metadata 的 `hash` 欄**不是**內容的 md5
（Synology 自己的同步雜湊，實測不符），完整性用 `size` 對帳。

## 學習模型

移植自 kg，**不是** SM-2 / Leitner / FSRS：二元評分、乘法式間隔、單位小時、無 ease factor。

```
base = max(current_interval, min)
next = clamp(base × (答對 ? 2.5 : 0.5), min, max)
```

| preset | 首次 | 答對 × | 答錯 × | 下限 | 上限 |
|---|---|---|---|---|---|
| relaxed（預設） | 24h | 2.5 | 0.5 | 6h | 1440h（60 天） |
| intensive | 8h | 1.4 | 0.35 | 4h | 1440h |
| custom | 沿用切換當下那組 | 1.05–5 | 0.1–0.95 | 沿用 | 沿用 |

`custom` 只開放兩個倍率，切過去時用當下那組 preset 當種子（所以第一眼跟切之前
一樣）。邊界不是美觀問題：答對倍率 ≤ 1 的話間隔永遠不成長、卡全部黏在下限，
答錯倍率 ≥ 1 則是「忘了」不縮短間隔 —— 兩者都讓排程失去意義，所以是硬夾擠
（超出範圍收回邊界，不回 400）。

**改倍率只對之後的作答有用，不追溯改寫過去。** 這在「事件日誌 + 整段重算」的
系統裡不是免費的：具名 preset 的事件只存名字（名字對應的數值永遠不變），
而 custom 的數值會被改，所以 custom 的事件把當時那五個值一起存進
`review_event.preset_params`（前端的本機事件是 `ev.preset_params`）。
`tests/test_custom_preset.py::test_changing_multipliers_does_not_rewrite_history`
釘住這件事。

三個不能改的細節（`tests/test_srs.py` 把每個分支釘死）：

1. **答錯是 ×0.5，不是歸零**。排到 30 天的卡答錯回到 15 天，不會被打回新卡。這跟 Anki 的 relearning queue 是不同哲學。
2. **夾擠順序**：先 `max(current, min)` 再乘、再夾回。下限的卡答錯是 `6×0.5=3 → 夾回 6h`。
3. **首次複習用 initial 當 base**；下次到期從**作答當下**起算，不是從原到期日。

卡片狀態是**算出來的**，不存欄位：`review_count==0 → 未學習`、`next_review_at<=now → 到期`、否則已複習。

三個入口（照 kg 的 Menu 結構）：未學 / 到期 / 全部。**刻意沒有每日上限**。

### 選擇題與二元評分

答對／答錯本身就是客觀訊號，直接映射 remembered／forgot，不需要 Anki 四鍵自評。
但三選一有 33% 猜對率，所以答對後可按**「我是猜的」**改判成 forgot。

改判走 `POST /api/answer/regrade`，用事件裡的 before 快照把卡片精確回捲後重打 —— 不是再送一次 answer（那會從已放大的 60h 再乘 0.5 得到 30h，且 `review_count` 多算一次）。

## 開發

```bash
uv sync
uv run pytest                 # SRS + API + 帳號 + 遷移 + 跨裝置收斂
bash deploy/run-server.sh     # 本機（沒有 secrets 檔就是開發模式）
```

`.github/workflows/ci.yml` 在每次 push 跑 `pytest` 與 `tools/verify.py`。
`ops/deploy.sh` 在重啟服務之前也會跑一次 `pytest` —— 測試不過就不會換版。

### 環境變數

| 變數 | 預設 | 作用 |
|---|---|---|
| `DL_DATA_DIR` | `~/driver-license-data` | 進度 DB 的位置 |
| `DL_ENV` | `dev` | `prod` 會關掉 `/api/docs` **與 `/openapi.json`** |
| `DL_SECRETS_FILE` | `~/.secrets/driver-license.env` | 上面這些的來源，不進 repo |
| `DL_TURNSTILE_SITEKEY` / `_SECRET` | 未設 | 兩個都設才啟用註冊的人機驗證。**只設 secret 會讓所有註冊拿到 400** |
| `DL_LEGACY_USERNAME` | `max` | v1→v2 遷移時，既有進度掛到哪個帳號名下 |
| `DL_VIDEO_ORIGIN` | 未設（＝同源 `/videos`） | 危險感知影片的來源。設成 `https://videos.wordnexus.lol` 就改由 R2 直出 |

`DL_VIDEO_ORIGIN` 一個變數同時決定三件事，所以不能只改前端：`/api/config` 下發給
前端組網址、**CSP 的 `media-src` 與 `img-src` 開這個口**、service worker 因跨來源
而自動略過。少了 CSP 那半邊，`<video>` 會被擋成一塊放不出來的黑框，
而**除了 console 以外沒有任何跡象**。

啟動時會往 stderr 印一行 `driver-license: env=… docs=… turnstile=…`。
secrets 檔漏了或 `DL_ENV` 打錯，就靠這行看出來。

## 身分模型

兩種，刻意的：

- **匿名**（預設）—— 題庫、SRS、狀態全部在瀏覽器的 IndexedDB 裡，伺服器一列都不寫。
  公網上的未認證流量因此碰不到 DB。代價是清除瀏覽資料就沒了（設定頁可匯出 JSON）。
- **帳號** —— 只為了跨裝置同步而存在。username + 密碼，**沒有 email**；
  救援碼在註冊時顯示一次，那是忘記密碼時唯一的後路。

自建而不接 Google 的理由：PWA 裡的 OAuth 轉址在 iOS 上會離開 standalone 情境，
回來時不一定回得到原本那個 app。表單登入沒有轉址，CSP 也就能維持 `default-src 'self'`。

### 認領遷移過來的進度

v1→v2 遷移建出來的帳號 `password_hash` 是 NULL，**沒有任何 HTTP 端點能設定它**
（註冊端點會拒絕已存在的帳號）。第一次部署後必須跑一次：

```bash
uv run python tools/useradmin.py list
uv run python tools/useradmin.py set-password max
```

沒跑這一步，舊進度就是沒有人登得進去。

進度 DB 在 **repo 外**（`$DL_DATA_DIR`，預設 `~/driver-license-data/app.db`），`git pull` / `reset` 不會動到 —— 但**服務啟動時會就地遷移它**，見下。

## 資料庫版本

`PRAGMA user_version` 記 schema 版本，目前是 **4**。
v2 = 多租戶（每列都掛 `user_id`）、v3 = `review_event` 記下作答當下的 preset
（重算歷史時要用「當時」的參數，否則換一次 preset 就會把過去追溯改寫）、
v4 = `review_event.preset_params`，custom 那一刻的五個數值快照（名字查不到
會變的數值，見「學習模型」）。
`app.main` 一被 import 就跑 `db.init()`，需要時就地遷移，**不可逆**（v1 的舊表會被 drop）。
認不得的版本會直接拒絕開啟，不會靜默蓋章。

## 部署（felix）

```bash
ssh chenliangyu@100.118.39.104
cd ~/project/driver-license
bash ops/deploy.sh
```

### 備份與還原

每日 11:20 `ops/backup.sh`（`com.driverlicense.backup`）：`sqlite3 .backup` 取一致快照 →
`integrity_check` → 帳號數閘 → 本地留 14 份 + 上傳 S3。

每月 1 號 11:40 `ops/restore-drill.sh --s3`（`com.driverlicense.drill`）：從 S3 抓最新那份
真的還原、真的起一個 `:8007` 的實例、打 `/api/health`、對帳帳號數與事件數。
全程不碰生產，不需要停機。**備份最貴的失敗模式是「一直在跑、也一直是綠的，
直到要用的那天才發現還原不回來」** —— 所以驗的是異地那份（本地那份跟生產同一顆碟）。

結果各留一行在 `~/Library/Logs/driver-license-backup.log` 與 `-drill.log`。

### 換版

`ops/deploy.sh` 依序做：前置檢查（服務在不在、工作區乾不乾淨）→ 備份 →
`git pull && uv sync` → `pytest` → `kickstart` → 打 `/api/health` 確認程序活著、
DB 開得起來、schema 遷到程式碼期待的版本。任何一步失敗就停下來並說明下一步。

**備份要在 `git pull` 之前**：`ops/backup.sh` 的健檢閘跟著 schema 走，先 pull 的話
新版腳本會對上還沒遷移的舊 DB，直接 exit 5。這個順序約束正是把它寫成腳本的理由。

**不要用 `/api/decks` 當 smoke check**：它掛了 `CurrentUser`，不帶 cookie 恆為 401 ——
這一步曾經寫成「應為 200」，等於從來沒有真的驗到 app 有沒有起來。要驗用
`/api/health`（不需認證，回 `ok` / `schema_version` / 題庫版本與題數）。

⚠️ 服務的 `WorkingDirectory` 就是這個 repo，`KeepAlive=true` —— uvicorn 直接 import
工作目錄裡的 `app/`。**在 felix 上編輯這個 repo 等於在動生產環境**：程序只要重啟一次
（崩潰、kickstart、開機）就會載入當下磁碟上的程式碼，包含寫到一半的版本，並且照樣跑
`db.init()`。要做長時間的修改，先 `launchctl bootout gui/$(id -u)/com.driverlicense.server`。

- 改 `app/*.py` → `kickstart -k`
- 改 `static/*`、`data/*` → 免重啟（StaticFiles / 啟動時載入的除外，改 JSON 仍需重啟）
- 改 `deploy/*.plist` → `cp` 到 `~/Library/LaunchAgents/` 後 **`bootout` 再 `bootstrap`**（`kickstart` 不重讀 plist；`bootout` 是非同步，太快 bootstrap 會 `I/O error 5`）

綁 `127.0.0.1:8006`，**不要**綁 `0.0.0.0` 或 `100.x`。唯一的入口是前面那兩個反向代理，
可路由介面上不該有任何開放埠。

**不要加 `--proxy-headers`**：uvicorn 的 ProxyHeadersMiddleware 會直接改寫
`scope["client"]`，於是 `ratelimit.client_ip` 那道「peer 是 loopback 才信任代理標頭」
的閘永遠不成立（實測帶 `X-Forwarded-For: 9.9.9.9` 時 `request.client.host` 就變成
`9.9.9.9`）。保持 peer 是真實的 `127.0.0.1`，代理標頭的信任由我們自己判。

## Cloudflare（公網那條路）

走既有的 `kg-standby` tunnel，設定步驟見 `~/butler/docs/cloudflare-tunnel-hosting.md` §5。
本專案要的是：ingress `drive.wordnexus.lol → http://localhost:8006`，DNS proxied CNAME。

**zone 設定必須確認關閉**，這四項是 CF 上 PWA 最常見的災難來源：

| 設定 | 為什麼 |
|---|---|
| Rocket Loader | 會重排 script 的執行順序，打壞原生 ESM module 載入 |
| Email Obfuscation | 往 HTML 注入 script，違反 `script-src 'self'` |
| Bot Fight Mode | 對 HTML 注入 JS 挑戰，打壞 PWA 導覽與 service worker |
| Mirage | 改寫 img，與 immutable 的題目圖互相打架 |

快取由源站的 `cache-control` 決定，不需要 Page Rule：
`/images/*` 與帶對版號的 `/data/*.json` 是 `immutable`（一年），其餘一律 `no-cache`。

### 驗證

⚠️ **用 curl 驗「CF 有沒有動我們的 HTML」是驗不出來的。** CF 的 HTML 改寫只對
「看起來像瀏覽器」的請求生效，curl 拿到的是原封不動的原始碼。實際踩過：
上線後 curl 與源站逐位元組相同，但瀏覽器裡多了一個 Cloudflare Web Analytics 的
beacon script（被我們的 CSP 擋下、沒有執行，但標籤確實被插進去了）。
要驗就得開瀏覽器看 `document.scripts`。

```bash
curl -sI https://drive.wordnexus.lol/images/car/C0001_1.png | grep -i cache-control   # immutable
curl -s -o /dev/null -w '%{http_code}\n' https://drive.wordnexus.lol/api/state       # 401
curl -s -o /dev/null -w '%{http_code}\n' https://drive.wordnexus.lol/openapi.json    # 404（prod）
curl -s -o /dev/null -w '%{http_code}\n' https://drive.wordnexus.lol/privacy         # 200
```
