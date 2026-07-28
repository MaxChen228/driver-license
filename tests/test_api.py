"""API 端到端測試。用臨時 DL_DATA_DIR，不碰真實進度。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client(module_client):
    """本檔的測試刻意共用一份 DB，前面答過的題會影響後面的計數。"""
    return module_client


def test_decks_report_full_bank_all_unlearned(client):
    d = client.get("/api/decks").json()
    by = {x["deck"]: x for x in d["decks"]}
    assert by["car"]["total"] == 1090
    assert by["moto"]["total"] == 806
    # 全新 DB：全部未學習
    assert by["car"]["unlearned"] == 1090 and by["car"]["due"] == 0
    assert d["preset"] == "relaxed"


def test_categories_match_printed_totals(client):
    """汽車三架構 424/230/436 是 PDF 自印的合計，拿來當抽取正確性的外部驗證。"""
    car = next(x for x in client.get("/api/decks").json()["decks"] if x["deck"] == "car")
    per_arch = {}
    for c in car["categories"]:
        per_arch[c["architecture"]] = per_arch.get(c["architecture"], 0) + c["count"]
    assert sorted(per_arch.values()) == [230, 424, 436]


def test_queue_unlearned_then_answer_moves_card_out(client):
    q = client.get("/api/queue/unlearned", params={"deck": "car"}).json()
    assert q["count"] == 1090
    cid = q["card_ids"][0]

    card = client.get(f"/api/card/{cid}").json()
    assert card["state"] == "unlearned"
    correct = card["question"]["answer"]

    r = client.post("/api/answer", json={"card_id": cid, "chosen": correct}).json()
    assert r["correct"] is True and r["remembered"] is True
    assert r["interval_hours"] == 60.0  # relaxed 首答答對：24 × 2.5

    assert client.get("/api/queue/unlearned", params={"deck": "car"}).json()["count"] == 1089
    assert client.get(f"/api/card/{cid}").json()["state"] == "reviewed"


def test_wrong_answer_records_lapse_and_shrinks(client):
    cid = client.get("/api/queue/unlearned", params={"deck": "moto"}).json()["card_ids"][0]
    correct = client.get(f"/api/card/{cid}").json()["question"]["answer"]
    wrong = next(x for x in (1, 2, 3) if x != correct)

    r = client.post("/api/answer", json={"card_id": cid, "chosen": wrong}).json()
    assert r["correct"] is False
    assert r["interval_hours"] == 12.0  # 24 × 0.5
    assert r["lapse_count"] == 1 and r["streak"] == 0


def test_guessed_overrides_a_correct_answer_to_forgot(client):
    """三選一有 33% 猜對率 —— 按了「我是猜的」就不該被排到 60 小時後。"""
    cid = client.get("/api/queue/unlearned", params={"deck": "moto"}).json()["card_ids"][0]
    correct = client.get(f"/api/card/{cid}").json()["question"]["answer"]

    r = client.post(
        "/api/answer", json={"card_id": cid, "chosen": correct, "guessed": True}
    ).json()
    assert r["correct"] is True and r["remembered"] is False
    assert r["interval_hours"] == 12.0


def test_regrade_rewinds_instead_of_double_counting(client):
    """答完才按「我是猜的」：必須改判上一次，不是再記一次。

    再送一次的話會從已放大的 60h 再乘 0.5 得到 30h，且 review_count 變 2 —— 都是錯的。
    """
    cid = client.get("/api/queue/unlearned", params={"deck": "car"}).json()["card_ids"][-1]
    correct = client.get(f"/api/card/{cid}").json()["question"]["answer"]

    first = client.post("/api/answer", json={"card_id": cid, "chosen": correct}).json()
    assert first["interval_hours"] == 60.0

    r = client.post("/api/answer/regrade", json={"card_id": cid}).json()
    assert r["remembered"] is False
    assert r["interval_hours"] == 12.0        # 24 × 0.5，從原始狀態重算
    assert r["lapse_count"] == 1 and r["streak"] == 0

    detail = client.get(f"/api/card/{cid}").json()
    assert detail["review"]["review_count"] == 1   # 不是 2
    assert detail["review"]["interval_hours"] == 12.0


def test_regrade_is_rejected_when_there_is_nothing_to_change(client):
    cid = "C0999"
    assert client.post("/api/answer/regrade", json={"card_id": cid}).status_code == 409
    correct = client.get(f"/api/card/{cid}").json()["question"]["answer"]
    wrong = next(x for x in (1, 2, 3) if x != correct)
    client.post("/api/answer", json={"card_id": cid, "chosen": wrong})
    assert client.post("/api/answer/regrade", json={"card_id": cid}).status_code == 409


def test_note_roundtrip_and_delete(client):
    cid = "C0001"
    client.put(f"/api/note/{cid}", json={"body": "  禁止四輪以上汽車及機車  "})
    assert client.get(f"/api/note/{cid}").json()["body"] == "禁止四輪以上汽車及機車"
    assert client.get(f"/api/card/{cid}").json()["note"] == "禁止四輪以上汽車及機車"
    client.put(f"/api/note/{cid}", json={"body": "   "})
    assert client.get(f"/api/note/{cid}").json()["body"] == ""


def test_browse_filters(client):
    all_items = client.get("/api/browse", params={"deck": "moto"}).json()
    assert all_items["count"] == 806
    wrong = client.get("/api/browse", params={"deck": "moto", "wrong_only": True}).json()
    assert 0 < wrong["count"] < 806
    assert all(i["lapsed"] for i in wrong["items"])
    hit = client.get("/api/browse", params={"deck": "moto", "q": "安全帽"}).json()
    assert hit["count"] > 0


def test_image_questions_are_served(client):
    """95 + 126 題題幹是圖，圖檔掛不上去這些題就是空白的。"""
    car = client.get("/api/browse", params={"deck": "car"}).json()["items"]
    img_q = next(i for i in car if i["stem_is_image"])
    detail = client.get(f"/api/card/{img_q['id']}").json()["question"]
    assert detail["images"]
    r = client.get("/" + detail["images"][0]["file"].replace("images/", "images/"))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_image_option_questions_flagged(client):
    """汽車 6 題的選項本身是圖，前端要靠 is_image 走不同分支。"""
    q = client.get("/api/card/C0183").json()["question"]
    assert all(o["is_image"] for o in q["options"])
    assert len(q["images"]) == 3


def test_pause_freezes_the_clock(client):
    s = client.put("/api/settings", json={"paused": True}).json()
    assert s["paused"] is True and s["paused_at"]
    client.put("/api/settings", json={"paused": False})


def test_stats_shape(client):
    s = client.get("/api/stats", params={"deck": "moto"}).json()
    assert s["answered_total"] >= 2
    assert len(s["forecast_14d"]) == 14
    assert s["streak_days"] >= 1
    assert 0.0 <= s["accuracy"] <= 1.0


def test_app_shell_is_never_heuristically_cached(client):
    """app shell 的每個檔案都必須明示 cache-control。

    少了它，瀏覽器會用「啟發式新鮮度」自己決定有效期（last-modified 至今的
    10%），而 service worker 安裝時的 addAll() 也會先問 HTTP 快取 —— 結果是
    新版 shell 把舊的 CSS 裝進新的 Cache Storage，新 HTML 配舊樣式，而且會
    留在那個版本裡直到下次改版。踩過一次，所以釘在測試裡。
    """
    for path in ["/", "/sw.js", "/static/style.css", "/static/app.js",
                 "/static/srs.js", "/static/store.js", "/data/car.json",
                 "/manifest.webmanifest"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path


def test_service_worker_shell_list_matches_disk(client):
    """sw.js 列的 SHELL 每個 URL 都要真的取得到。

    少一個檔案 addAll() 會整批失敗（規格如此），結果是 SW 永遠裝不起來、
    離線功能靜靜地失效，而且不會有任何錯誤訊息浮到畫面上。
    """
    import re

    sw = client.get("/sw.js").text
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    urls = re.findall(r'"([^"]+)"', shell)
    assert "/static/style.css" in urls and "/static/fonts/archivo-latin.woff2" in urls
    for u in urls:
        assert client.get(u).status_code == 200, u


def test_every_css_variable_is_defined():
    """style.css 不得引用任何沒定義過的自訂屬性。

    CSS 對未定義的 var() 是 invalid-at-computed-value-time：整個宣告退成 unset，
    邊框直接消失、顏色掉回 inherit，而且完全沒有錯誤訊息 —— 靠肉眼看不出來。
    commit 243ce79 修過一次（--line / --muted / --accent 三個不存在的名字讓救援碼
    那個「刻意要擋住流程」的框完全沒有框），當時只做了一次性的人工檢查，
    後來又再犯一次。這次釘進測試。

    帶 fallback 的 var(--x, ...) 一樣要求 --x 存在：fallback 是給「這個值可能
    沒設」用的，不是給打錯字用的。
    """
    import re

    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\(\s*(--[\w-]+)", css))
    assert not (used - defined), f"用了沒定義的自訂屬性：{sorted(used - defined)}"


def test_video_origin_opens_the_csp_it_needs(tmp_path, monkeypatch):
    """影片搬到 R2 之後，CSP 必須同時開 media-src 與 img-src。

    這是會安靜壞掉的那種：media-src 沒寫就 fallback 到 default-src 'self'，
    <video> 被擋成一塊放不出來的黑框，console 以外沒有任何跡象。poster 走的是
    img-src 不是 media-src，兩個都要開 —— 只開一個的話影片能播但清單縮圖全空。

    同時釘住預設（未設環境變數）不得有 media-src：內網／開發是同源供應，
    多開一個口沒有理由。
    """
    import importlib

    from tests.conftest import data_dir_env

    def csp_and_config(origin):
        if origin is None:
            monkeypatch.delenv("DL_VIDEO_ORIGIN", raising=False)
        else:
            monkeypatch.setenv("DL_VIDEO_ORIGIN", origin)
        with data_dir_env(tmp_path / (origin or "none").replace("/", "_")):
            import app.db
            import app.main

            importlib.reload(app.db)
            main = importlib.reload(app.main)
            c = TestClient(main.app, base_url="https://testserver")
            return c.get("/").headers["content-security-policy"], \
                c.get("/api/config").json()["video_origin"]

    csp, origin = csp_and_config(None)
    assert origin == ""
    assert "media-src" not in csp

    r2 = "https://videos.wordnexus.lol"
    csp, origin = csp_and_config(r2)
    assert origin == r2
    assert f"media-src 'self' {r2}" in csp
    assert f"img-src 'self' data: {r2}" in csp

    # 尾斜線不該產生 "…lol//videos/x.mp4"
    _, origin = csp_and_config(r2 + "/")
    assert origin == r2


def test_bank_and_images_are_cacheable_at_the_edge(client):
    """公網上這是主要的頻寬防線。

    354 張圖 1.8 MB + 1.5 MB 題庫，每個新訪客都回原站的話走的是家用上行。
    原本 /images 也被強制 no-cache（那是為 app shell 訂的規則），CF 邊緣因此
    一張都不會替我們擋。
    """
    img = client.get("/images/car/C0001_1.png")
    assert img.status_code == 200
    assert img.headers["cache-control"] == "public, max-age=31536000, immutable"

    version = next(
        d["version"] for d in client.get("/api/config").json()["decks"] if d["deck"] == "car"
    )
    ok = client.get("/data/car.json", params={"v": version})
    assert ok.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_a_wrong_bank_version_is_never_given_a_long_cache(client):
    """版號對不上就退回 no-cache。

    給錯的內容長快取，等於在邊緣種一份永遠清不掉的錯誤 —— 而 ?v= 是誰都能填的。
    """
    for bad in [{}, {"v": "nonsense"}, {"v": "115.5.28"}]:
        r = client.get("/data/car.json", params=bad)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache", bad


def test_app_shell_stays_no_cache(client):
    """殼刻意不吃這一套。commit 21ec7ce 記錄過「新 shell 配舊 CSS」的實際事故，
    離線由 SW 負責，這裡不為了省流量退讓。"""
    for path in ["/", "/static/app.js", "/static/style.css", "/sw.js"]:
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_health_needs_no_auth_and_reports_schema_and_banks(anon_client):
    """部署後的 smoke check 打的就是這裡，所以它必須在「沒有 cookie」時就是 200。

    README 原本教人用 /api/decks 當這一步，但那個端點要認證、實際回 401 ——
    等於那一步從來沒驗到 app 有沒有起來。
    """
    r = anon_client.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # 遷移真的跑完了才算健康；部署腳本比對的就是這個數字
    assert body["schema_version"] == 4
    assert body["decks"]["car"]["questions"] == 1090
    assert body["decks"]["moto"]["questions"] == 806
    # 健康狀態被快取住的話這個端點就是在說謊
    assert r.headers["cache-control"] == "no-store"


def test_health_leaks_nothing_user_specific(anon_client):
    """公網上唯一不需要認證的動態端點 —— 回傳內容必須只有公開事實。"""
    body = anon_client.get("/api/health").json()
    assert set(body) == {"ok", "schema_version", "decks"}
    assert set(body["decks"]["car"]) == {"version", "questions"}


def test_app_starts_without_the_videos_directory(anon_client):
    """影片不進 git（data/videos/ 在 .gitignore），所以「沒跑過影片 pipeline」是
    全新 clone、CI 與 git worktree 的正常狀態。

    StaticFiles 預設會對不存在的目錄在建構時 raise —— 那會讓 import app.main
    整個失敗，而不是讓 /videos/* 404。實際踩過：99 個測試在 setup 就 error，
    死因是 RuntimeError: Directory '.../data/videos' does not exist。
    這個測試能通過本身就代表 app 在沒有影片的環境裡起得來。
    """
    from app.deck import DATA_DIR

    # app.main 被 import 就會把目錄補出來，所以這裡驗的是「它確實補了」
    assert (DATA_DIR / "videos").is_dir()
    assert anon_client.get("/api/health").status_code == 200
    # 缺席的影片要 404，不是 500
    assert anon_client.get("/videos/nope.mp4").status_code == 404
