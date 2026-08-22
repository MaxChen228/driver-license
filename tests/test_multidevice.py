"""兩台裝置分批補送時，事件的抵達順序不能影響最終狀態。

間隔是乘法式的，套用順序不可交換：同一張卡「對、錯、對」與「對、對、錯」
算出來的下次複習時間差好幾天。單一使用者一台裝置時事件天然照時間抵達，
所以這件事一直沒有代價；跨裝置同步一開，較舊的事件在較新的狀態之後抵達
就是常態 —— 而且算錯不會有任何跡象，只會安靜地把排程歪掉。
"""

import itertools

import pytest

from tests.conftest import _fresh_client

# t1 對、t2 錯、t3 對。挑這個序列是因為它對順序特別敏感：
#   fresh →對→ 168 →錯→ 168×0.5=84 →對→ 84×2.5=210
# 換成任何其他順序都不會是 210。
TIMES = [
    "2026-07-18T02:00:00+00:00",
    "2026-07-19T02:00:00+00:00",
    "2026-07-20T02:00:00+00:00",
]
CARD = "C0001"


def _events(client):
    answer = client.get(f"/api/card/{CARD}").json()["question"]["answer"]
    wrong = next(x for x in (1, 2, 3) if x != answer)
    return [
        {"card_id": CARD, "chosen": answer, "client_event_id": "e1", "reviewed_at": TIMES[0]},
        {"card_id": CARD, "chosen": wrong, "client_event_id": "e2", "reviewed_at": TIMES[1]},
        {"card_id": CARD, "chosen": answer, "client_event_id": "e3", "reviewed_at": TIMES[2]},
    ]


def _state(client):
    return next(s for s in client.get("/api/state").json()["states"] if s["id"] == CARD)


def test_in_order_delivery_is_the_reference_result(client):
    evs = _events(client)
    for e in evs:
        client.post("/api/sync", json={"answers": [e]})
    st = _state(client)
    assert st["interval_hours"] == 210.0         # 168 → ×0.5 → ×2.5
    assert st["review_count"] == 3
    assert st["lapse_count"] == 1
    assert st["streak"] == 1
    assert st["next_review_at"] == "2026-07-28T20:00:00+00:00"  # t3 + 210h


def test_every_arrival_order_converges(tmp_path_factory):
    """六種抵達順序，每種一份全新 DB，最終狀態必須完全相同。"""
    results = {}
    for perm in itertools.permutations(range(3)):
        with _fresh_client(tmp_path_factory.mktemp("perm")) as client:
            evs = _events(client)
            for i in perm:
                # 一筆一個請求：同一批內 /api/sync 本來就會照 reviewed_at 排序，
                # 分批送才是真正在測「跨請求的亂序」。
                client.post("/api/sync", json={"answers": [evs[i]]})
            results[perm] = _state(client)

    reference = results[(0, 1, 2)]
    for perm, st in results.items():
        assert st == reference, f"抵達順序 {perm} 的結果與依序抵達不同"
    assert reference["interval_hours"] == 210.0


def test_out_of_order_arrival_rewrites_event_snapshots(client):
    """重算必須連事件的 before/after 快照一起改寫。

    regrade 靠 before 精確回捲上一次作答；快照沒跟著重算的話，那些數字指向
    一個從來不存在的狀態，「我是猜的」就會把卡片送到一個莫名其妙的間隔去。
    """
    evs = _events(client)
    client.post("/api/sync", json={"answers": [evs[2]]})  # 先送最新的
    client.post("/api/sync", json={"answers": [evs[0]]})  # 再送最舊的
    client.post("/api/sync", json={"answers": [evs[1]]})  # 中間那筆最後到

    assert _state(client)["interval_hours"] == 210.0

    # e3（t3，答對）現在是時間序最後一筆，但它是「第一個被寫進 DB 的」。
    # regrade 改判它為 forgot：正確的 before 是 e2 之後的狀態 interval=84，
    # 於是 84×0.5=42。若快照沒被重算，這裡會拿到當初首答答對的舊值。
    r = client.post("/api/answer/regrade", json={"card_id": CARD}).json()
    assert r["interval_hours"] == 42.0
    assert r["review_count"] == 3
    assert r["lapse_count"] == 2


def test_regrade_targets_the_latest_answer_by_time_not_by_insertion(client):
    """「我剛剛那次是猜的」指的是時間上的最後一次，不是最後寫進 DB 的那一筆。

    亂序補送之後這兩者會分家。用插入順序挑的話，這裡會挑到 t2 那筆答錯的事件，
    回 409「already graded as forgot」—— 使用者按了「我是猜的」卻什麼都沒發生。
    """
    evs = _events(client)
    client.post("/api/sync", json={"answers": [evs[2]]})  # t3 先到
    client.post("/api/sync", json={"answers": [evs[1]]})  # t2 後到，是最後插入的那筆

    r = client.post("/api/answer/regrade", json={"card_id": CARD})
    assert r.status_code == 200, r.json()
    assert r.json()["remembered"] is False


def test_repeated_delivery_of_the_same_batch_changes_nothing(client):
    """補送是冪等的：同一批重送幾次，結果都不能變。"""
    evs = _events(client)
    client.post("/api/sync", json={"answers": [evs[2], evs[0], evs[1]]})
    once = _state(client)
    for _ in range(3):
        client.post("/api/sync", json={"answers": evs})
    assert _state(client) == once


def test_batch_touching_one_card_many_times_rebuilds_once(client, monkeypatch):
    """整批送達時亂序重算要延到迴圈外做，不能每碰一次算一次。"""
    import app.db

    calls = []
    original = app.db.rebuild_card
    monkeypatch.setattr(
        app.db, "rebuild_card",
        lambda *a, **k: (calls.append(a[2]), original(*a, **k))[1],
    )

    evs = _events(client)
    client.post("/api/sync", json={"answers": [evs[2]]})
    r = client.post("/api/sync", json={"answers": [evs[1], evs[0]]}).json()
    assert r["sync"]["applied"] == ["e1", "e2"]  # 批內仍照 reviewed_at 排序
    assert _state(client)["interval_hours"] == 210.0
    # 兩筆都亂序、都碰同一張卡，但整批只該重算一次
    assert calls == [CARD]


def test_in_order_answers_never_rebuild(client, monkeypatch):
    """快路徑要保住：照時間抵達就不該回頭重算整段歷史。

    全面重算會讓「換一次 preset」把過去追溯改寫，那是使用者沒要求的事。
    """
    import app.db

    calls = []
    monkeypatch.setattr(
        app.db, "rebuild_card", lambda *a, **k: calls.append(a[2]),
    )
    for e in _events(client):
        client.post("/api/sync", json={"answers": [e]})
    assert calls == []


def test_pause_does_not_collapse_the_history_of_backfilled_answers(client):
    """暫停期間補送舊作答，仍必須收斂。

    凍結時鐘原本會把每一筆補送的 reviewed_at 都改寫成 paused_at，於是整批事件
    共用同一個時刻：亂序偵測的 `>` 永遠不成立，重算的順序退化成插入順序，
    收斂保證在暫停期間整個失效。凍結時鐘要凍的是「現在幾點」，
    不是「那件事什麼時候發生的」。
    """
    evs = _events(client)
    client.put("/api/settings", json={"paused": True})

    for i in (2, 0, 1):  # 亂序補送
        client.post("/api/sync", json={"answers": [evs[i]]})

    st = _state(client)
    assert st["interval_hours"] == 210.0
    assert st["review_count"] == 3 and st["lapse_count"] == 1 and st["streak"] == 1
    # 每一天各自記帳，不能全部塌成暫停那一天
    activity = client.get("/api/stats", params={"deck": "car"}).json()["activity"]
    assert [a["day"] for a in activity] == ["2026-07-18", "2026-07-19", "2026-07-20"]


def test_changing_preset_does_not_rewrite_older_history(client):
    """換 preset 之後，任何一筆遲到的舊事件都不該把整張卡改寫。

    重算若一律用「現在的」參數：relaxed 下答對三次是 1050h，切到 intensive 後
    來一筆亂序的舊事件會整段重算成 460.99h —— 使用者只是換過設定，卡片卻悄悄
    搬家，而且最終狀態重新取決於抵達順序。每筆事件用它自己的 preset 才對。
    """
    answer = client.get(f"/api/card/{CARD}").json()["question"]["answer"]
    for i, t in enumerate(TIMES):
        client.post("/api/sync", json={"answers": [
            {"card_id": CARD, "chosen": answer, "client_event_id": f"r{i}",
             "reviewed_at": t},
        ]})
    assert _state(client)["interval_hours"] == 1050.0  # 168 × 2.5²

    client.put("/api/settings", json={"preset": "intensive"})

    # 一筆遲到的舊事件（時間落在最前面），觸發整段重算
    client.post("/api/sync", json={"answers": [
        {"card_id": CARD, "chosen": answer, "client_event_id": "late",
         "reviewed_at": "2026-07-17T02:00:00+00:00"},
    ]})
    # 重算後的鏈：late 用 intensive（首答對仍是 168h），另外三筆維持 relaxed
    #   168 →×2.5→ 420 →×2.5→ 1050 →×2.5→ cap 1440
    # 插進一筆更早的事件本來就會讓整條鏈重算，1440 ≠ 1050 是正常的。
    st = _state(client)
    assert st["review_count"] == 4
    assert st["interval_hours"] == 1440.0
    # 關鍵：不能是 460.99 —— 那是「整段歷史都用現在的 intensive 重算」的結果
    #   168 →×1.4→ 235.2 →×1.4→ 329.28 →×1.4→ 460.99
    assert st["interval_hours"] != pytest.approx(460.99, abs=0.01)


def test_events_sort_by_instant_not_by_text(client):
    """reviewed_at 是前端給的，時區偏移原樣存進 DB —— 文字序不等於時間序。

    '2026-07-20T10:00:00+08:00'（＝02:00 UTC）在文字上排在
    '2026-07-20T03:00:00+00:00' 之後，實際上卻早一小時。SQL 裡 ORDER BY
    reviewed_at 就會把兩筆的先後對調，重算出來的 streak / last_feedback 全反。
    """
    answer = client.get(f"/api/card/{CARD}").json()["question"]["answer"]
    wrong = next(x for x in (1, 2, 3) if x != answer)
    # 真實順序：先答對（02:00 UTC），再答錯（03:00 UTC）
    early_correct = {"card_id": CARD, "chosen": answer, "client_event_id": "tz1",
                     "reviewed_at": "2026-07-20T10:00:00+08:00"}
    later_wrong = {"card_id": CARD, "chosen": wrong, "client_event_id": "tz2",
                   "reviewed_at": "2026-07-20T03:00:00+00:00"}

    client.post("/api/sync", json={"answers": [later_wrong]})   # 晚的先到
    client.post("/api/sync", json={"answers": [early_correct]})  # 早的後到，觸發重算

    st = _state(client)
    # 正確順序（對→錯）：streak 歸零、最後一次是 forgot
    assert st["streak"] == 0
    assert st["last_feedback"] == 0
    assert st["lapse_count"] == 1
