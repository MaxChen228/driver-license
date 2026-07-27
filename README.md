# driver-license

台灣汽機車駕照筆試刷題 app。題庫從監理所公告 PDF 抽出，複習排程移植自 `~/project/kg` 的 flash card system。

部署在 felix 常駐機、走 Tailscale 內網：**http://100.118.39.104:8006**（tailnet only，無對外）。

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
uv run pytest                                              # SRS + API
uv run uvicorn app.main:app --host 127.0.0.1 --port 8006   # 本機
```

進度 DB 在 **repo 外**（`$DL_DATA_DIR`，預設 `~/driver-license-data/app.db`），`git pull` / `reset` 不會動到 —— 但**服務啟動時會就地遷移它**，見下。

## 資料庫版本

`PRAGMA user_version` 記 schema 版本，目前是 **2**（多租戶：每列都掛 `user_id`）。
`app.main` 一被 import 就跑 `db.init()`，需要時就地遷移，**不可逆**（v1 的舊表會被 drop）。
認不得的版本會直接拒絕開啟，不會靜默蓋章。

## 部署（felix）

```bash
ssh chenliangyu@100.118.39.104
cd ~/project/driver-license
bash ops/backup.sh                 # 先備份：下一步可能觸發不可逆的 schema 遷移
git pull && uv sync
launchctl kickstart -k gui/$(id -u)/com.driverlicense.server
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8006/api/decks   # 應為 200
```

**備份要在 `git pull` 之前**：`ops/backup.sh` 的健檢閘跟著 schema 走，先 pull 的話
新版腳本會對上還沒遷移的舊 DB，直接 exit 5。

⚠️ 服務的 `WorkingDirectory` 就是這個 repo，`KeepAlive=true` —— uvicorn 直接 import
工作目錄裡的 `app/`。**在 felix 上編輯這個 repo 等於在動生產環境**：程序只要重啟一次
（崩潰、kickstart、開機）就會載入當下磁碟上的程式碼，包含寫到一半的版本，並且照樣跑
`db.init()`。要做長時間的修改，先 `launchctl bootout gui/$(id -u)/com.driverlicense.server`。

- 改 `app/*.py` → `kickstart -k`
- 改 `static/*`、`data/*` → 免重啟（StaticFiles / 啟動時載入的除外，改 JSON 仍需重啟）
- 改 `deploy/*.plist` → `cp` 到 `~/Library/LaunchAgents/` 後 **`bootout` 再 `bootstrap`**（`kickstart` 不重讀 plist；`bootout` 是非同步，太快 bootstrap 會 `I/O error 5`）

綁 `100.118.39.104:8006`，**不要**綁 `0.0.0.0`。tailnet 即邊界，無認證，**絕不掛 Cloudflare Tunnel**。
