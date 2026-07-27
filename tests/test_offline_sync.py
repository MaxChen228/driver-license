"""離線同步的端到端測試。

這裡釘死的是離線功能會壞掉的那幾種方式：補送重覆算分、補送時間全部塌成同一刻、
補送順序錯亂導致間隔算錯。這三件事在正常使用時看不出來，只會安靜地把排程算歪。
"""

from datetime import datetime, timedelta, timezone

def _answer_for(client, cid):
    return client.get(f"/api/card/{cid}").json()["question"]["answer"]


def test_bootstrap_carries_everything_needed_offline(client):
    b = client.get("/api/bootstrap").json()
    # states 只含答過的卡（惰性建列）。前端把「沒列出的」一律當 unlearned，
    # 所以全新帳號的 bootstrap 是空的，而不是 1896 筆全預設值。
    assert b["states"] == []
    assert {d["deck"] for d in b["decks"]} == {"car", "moto"}
    assert [d["url"] for d in b["decks"]] == ["/data/car.json", "/data/moto.json"]
    # 前端要靠 params 自己跑排程，缺一個就算不出下次複習時間
    assert set(b["params"]) == {
        "initial_hours", "remembered_mult", "forgot_mult", "min_hours", "max_hours"
    }
    assert b["params"]["initial_hours"] == 24.0


def test_deck_json_is_served_for_precache(client):
    r = client.get("/data/car.json")
    assert r.status_code == 200
    assert len(r.json()["questions"]) == 1090
    assert client.get("/data/nope.json").status_code == 404


def test_image_manifest_lists_every_png(client):
    m = client.get("/api/image-manifest").json()
    assert m["count"] == 354
    assert all(f.startswith("/images/") for f in m["files"])


def test_sync_applies_offline_answers_and_returns_fresh_state(client):
    cid = "C0001"
    correct = _answer_for(client, cid)
    r = client.post("/api/sync", json={
        "answers": [{
            "card_id": cid, "chosen": correct, "client_event_id": "e1",
            "reviewed_at": "2026-07-20T02:00:00+00:00",
        }],
        "notes": {cid: "離線寫的筆記"},
    }).json()

    assert r["sync"]["applied"] == ["e1"]
    st = next(s for s in r["states"] if s["id"] == cid)
    assert st["interval_hours"] == 60.0
    # 到期時間必須從「當初作答的時刻」起算，不是從補送的時刻
    assert st["next_review_at"].startswith("2026-07-22T14:00")
    assert r["notes"][cid] == "離線寫的筆記"


def test_duplicate_client_event_id_is_counted_once(client):
    """補送到一半斷線、使用者重新整理再送一次 —— 必然會發生，不能重複計分。"""
    cid = "C0002"
    ev = {"card_id": cid, "chosen": _answer_for(client, cid), "client_event_id": "dup"}

    first = client.post("/api/sync", json={"answers": [ev]}).json()
    assert first["sync"]["applied"] == ["dup"]

    second = client.post("/api/sync", json={"answers": [ev]}).json()
    assert second["sync"] == {"applied": [], "skipped": ["dup"], "failed": []}

    st = next(s for s in second["states"] if s["id"] == cid)
    assert st["review_count"] == 1          # 不是 2
    assert st["interval_hours"] == 60.0     # 不是 150（60 × 2.5）

    # 走 /api/answer 這條路也要擋
    assert client.post("/api/answer", json={**ev}).status_code == 409


def test_replay_order_follows_reviewed_at_not_arrival_order(client):
    """同一張卡答兩次，先後不同結果就不同 —— 順序錯了間隔就錯了。

    先答對（24×2.5=60）再答錯（60×0.5=30）。若順序反過來會是
    先錯（24×0.5=12）再對（12×2.5=30）—— 這裡碰巧同為 30，所以改用
    streak/lapse 與 last_feedback 來辨別，它們不對稱。
    """
    cid = "C0003"
    correct = _answer_for(client, cid)
    wrong = next(x for x in (1, 2, 3) if x != correct)
    t0 = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)

    # 故意把「比較晚」的那筆放在陣列前面
    r = client.post("/api/sync", json={"answers": [
        {"card_id": cid, "chosen": wrong, "client_event_id": "b",
         "reviewed_at": (t0 + timedelta(hours=1)).isoformat()},
        {"card_id": cid, "chosen": correct, "client_event_id": "a",
         "reviewed_at": t0.isoformat()},
    ]}).json()

    assert r["sync"]["applied"] == ["a", "b"]     # 依 reviewed_at 重排過
    st = next(s for s in r["states"] if s["id"] == cid)
    assert st["review_count"] == 2
    assert st["last_feedback"] == 0               # 最後一筆是答錯
    assert st["streak"] == 0 and st["lapse_count"] == 1
    assert st["interval_hours"] == 30.0           # 60 × 0.5


def test_offline_answers_keep_their_own_day_keys(client):
    """整批補送不能全部記成同一天，否則熱力圖與連續天數都是假的。"""
    days = ["2026-07-18", "2026-07-19", "2026-07-20"]
    client.post("/api/sync", json={"answers": [
        {"card_id": f"M000{i + 1}", "chosen": 1, "client_event_id": f"d{i}",
         "reviewed_at": f"{d}T03:00:00+00:00"}
        for i, d in enumerate(days)
    ]})
    activity = client.get("/api/stats", params={"deck": "moto"}).json()["activity"]
    assert [a["day"] for a in activity] == days


def test_failed_events_are_reported_not_silently_dropped(client):
    r = client.post("/api/sync", json={"answers": [
        {"card_id": "C9999", "chosen": 1, "client_event_id": "bad"},
    ]}).json()
    assert r["sync"]["applied"] == [] and r["sync"]["skipped"] == []
    assert r["sync"]["failed"][0]["client_event_id"] == "bad"


def test_sync_with_nothing_pending_is_just_a_refresh(client):
    client.post("/api/sync", json={"answers": [
        {"card_id": "C0001", "chosen": 1, "client_event_id": "x1"},
    ]})
    r = client.post("/api/sync", json={}).json()
    assert r["sync"] == {"applied": [], "skipped": [], "failed": []}
    # 空同步不會動到任何狀態，但仍要把權威快照整份帶回來
    assert [s["id"] for s in r["states"]] == ["C0001"]


def test_service_worker_is_served_from_root(client):
    """SW 掛在 /static/ 底下就只能管 /static/ 的請求，整個 app 都不會被攔截。"""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert client.get("/manifest.webmanifest").status_code == 200
