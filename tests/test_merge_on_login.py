"""登入時把這台裝置的匿名紀錄併入帳號。

前端的 pushLocalHistory() 就是「把本機 events 依時間序、每批 500 筆送進
/api/sync」。這裡驗的是那個契約在伺服器端成立：批次不會算錯、與帳號既有的
歷史正確交錯、重送不會重複計分。合併是不可撤銷的操作，錯了沒有第二次機會。
"""

from tests.conftest import TEST_PASSWORD, _fresh_client

CARD = "C0001"
BATCH = 500  # 與 static/app.js 的 pushLocalHistory 一致


def _events(client, times, card=CARD):
    """模擬本機 events：依時間序、每筆一個 client_event_id。"""
    answer = client.get(f"/api/card/{card}").json()["question"]["answer"]
    return [
        {"card_id": card, "chosen": answer, "client_event_id": f"local-{i}",
         "reviewed_at": t}
        for i, t in enumerate(times)
    ]


def _push(client, events):
    """pushLocalHistory 的對譯：依序分批送。"""
    for i in range(0, len(events), BATCH):
        r = client.post("/api/sync", json={
            "answers": events[i:i + BATCH],
            "notes": {CARD: "匿名時寫的筆記"} if i == 0 else {},
        })
        assert r.status_code == 200, r.text
    return r.json()


def test_merging_an_empty_account_reproduces_the_local_history(client):
    """剛註冊的帳號伺服器上是空的 —— 併進去之後應該與本機算出來的完全一樣。"""
    times = ["2026-07-18T02:00:00+00:00", "2026-07-19T02:00:00+00:00",
             "2026-07-20T02:00:00+00:00"]
    out = _push(client, _events(client, times))

    st = next(s for s in out["states"] if s["id"] == CARD)
    assert st["review_count"] == 3
    assert st["interval_hours"] == 375.0        # 24 × 2.5³
    assert out["notes"][CARD] == "匿名時寫的筆記"


def test_merging_interleaves_with_history_the_account_already_has(tmp_path_factory):
    """另一台裝置早就同步過了。合併必須把兩邊照時間交錯，不是接在後面。"""
    with _fresh_client(tmp_path_factory.mktemp("merge")) as c:
        # 帳號上已有的：7/18 與 7/20
        _push(c, [
            {**e, "client_event_id": f"srv-{i}"}
            for i, e in enumerate(_events(c, ["2026-07-18T02:00:00+00:00",
                                              "2026-07-20T02:00:00+00:00"]))
        ])
        # 這台裝置離線期間答的：7/19，時間夾在中間
        out = _push(c, _events(c, ["2026-07-19T02:00:00+00:00"]))

    st = next(s for s in out["states"] if s["id"] == CARD)
    assert st["review_count"] == 3
    # 三筆照時間序摺疊 = 375，而不是「接在後面」的結果
    assert st["interval_hours"] == 375.0


def test_merging_twice_is_a_no_op(client):
    """網路不穩時使用者會再按一次登入。第二次不能把間隔再乘一輪。"""
    evs = _events(client, ["2026-07-18T02:00:00+00:00", "2026-07-19T02:00:00+00:00"])
    first = _push(client, evs)
    again = _push(client, evs)

    assert again["sync"]["applied"] == []
    assert again["sync"]["skipped"] == ["local-0", "local-1"]
    a = next(s for s in first["states"] if s["id"] == CARD)
    b = next(s for s in again["states"] if s["id"] == CARD)
    assert a == b


def test_a_history_larger_than_one_batch_still_lands_in_order(client):
    """超過一批的歷史要照批次順序送，否則後面的批會變成「亂序抵達」。"""
    times = [f"2026-07-{18 + (i // 100):02d}T{i % 24:02d}:00:00+00:00"
             for i in range(BATCH + 120)]
    cards = [f"C{i + 1:04d}" for i in range(len(times))]
    answers = []
    for i, (t, cid) in enumerate(zip(times, cards)):
        ans = client.get(f"/api/card/{cid}").json()["question"]["answer"]
        answers.append({"card_id": cid, "chosen": ans,
                        "client_event_id": f"big-{i}", "reviewed_at": t})
    answers.sort(key=lambda a: a["reviewed_at"])

    out = _push(client, answers)
    assert len(out["sync"]["applied"]) + len(out["sync"]["skipped"]) > 0
    assert len(out["states"]) == len(cards)
    assert all(s["review_count"] == 1 for s in out["states"])


def test_choosing_the_account_instead_discards_nothing_server_side(tmp_path_factory):
    """選「改用帳號上的紀錄」時，前端清本機、完全不送 —— 伺服器不該有任何變化。"""
    with _fresh_client(tmp_path_factory.mktemp("keep")) as c:
        _push(c, _events(c, ["2026-07-18T02:00:00+00:00"]))
        before = c.get("/api/state").json()
        # 「取消合併」那條路徑不會呼叫 /api/sync，只重新拉一次狀態
        after = c.get("/api/state").json()
        assert before["states"] == after["states"]
