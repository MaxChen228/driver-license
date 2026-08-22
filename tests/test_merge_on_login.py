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
    assert st["interval_hours"] == 1050.0       # 168 × 2.5²
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
    # 三筆照時間序摺疊 = 1050，而不是「接在後面」的結果
    assert st["interval_hours"] == 1050.0


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


def test_a_history_larger_than_one_batch_is_fully_applied(client):
    """超過一批的歷史要一筆不漏地落地，而且結果與單批送出相同。

    刻意讓同一張卡跨越批次邊界被答很多次 —— 每張卡各答一次的話 review_count
    在任何情況下都是 1，測試等於什麼都沒驗。
    （批次「順序」本身不影響正確性，那是 Phase 3 的收斂保證負責的；
    這裡驗的是「620 筆真的全部進去了」。）
    """
    answer = client.get(f"/api/card/{CARD}").json()["question"]["answer"]
    n = BATCH + 120
    # 同一張卡連答 620 次，時間嚴格遞增，剛好跨過 500 的批次邊界
    answers = [
        {"card_id": CARD, "chosen": answer, "client_event_id": f"big-{i}",
         "reviewed_at": f"2026-07-18T00:00:00.{i:06d}+00:00"}
        for i in range(n)
    ]
    out = _push(client, answers)

    st = next(s for s in out["states"] if s["id"] == CARD)
    assert st["review_count"] == n
    assert st["streak"] == n            # 全部答對，一次都沒有被當成亂序而重算歸零
    assert st["interval_hours"] == 1440.0  # 早就撞到 60 天上限
    # 最後一筆的時間決定下次到期 —— 順序錯了這個值就會指向別筆
    assert st["next_review_at"].startswith("2026-09-16T00:00:00.000619")


def test_notes_survive_a_merge_with_no_answers_at_all(client):
    """一題都沒答、只寫過筆記的裝置，登入後筆記不能消失。

    題庫頁點開一題直接寫筆記就會落到這個狀態。前端的 pushLocalHistory 若因為
    「沒有事件」而整個迴圈跳過，就完全不會送出 notes，接著 /api/state 回一份空的、
    SNAP 被整份取代 —— 筆記安靜地被刪掉。合併要帶過去的東西裡，筆記正是其中一項。
    """
    out = client.post("/api/sync", json={"answers": [], "notes": {CARD: "只有筆記"}}).json()
    assert out["notes"] == {CARD: "只有筆記"}
    assert out["states"] == []          # 真的一題都沒答
    assert client.get("/api/state").json()["notes"] == {CARD: "只有筆記"}
