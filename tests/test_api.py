"""API 端到端測試。用臨時 DL_DATA_DIR，不碰真實進度。"""

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["DL_DATA_DIR"] = str(tmp_path_factory.mktemp("dl"))
    import app.db
    import app.main

    importlib.reload(app.db)
    main = importlib.reload(app.main)
    with TestClient(main.app) as c:
        yield c


def test_decks_seeded_with_full_bank(client):
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
