"""兩台裝置分批補送時，事件的抵達順序不能影響最終狀態。

間隔是乘法式的，套用順序不可交換：同一張卡「對、錯、對」與「對、對、錯」
算出來的下次複習時間差好幾天。單一使用者一台裝置時事件天然照時間抵達，
所以這件事一直沒有代價；跨裝置同步一開，較舊的事件在較新的狀態之後抵達
就是常態 —— 而且算錯不會有任何跡象，只會安靜地把排程歪掉。
"""

import itertools

from fastapi.testclient import TestClient

from tests.conftest import _fresh_client

# t1 對、t2 錯、t3 對。挑這個序列是因為它對順序特別敏感：
#   fresh(24) →對→ 24×2.5=60 →錯→ 60×0.5=30 →對→ 30×2.5=75
# 換成任何其他順序都不會是 75。
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
    return next(s for s in client.get("/api/bootstrap").json()["states"] if s["id"] == CARD)


def test_in_order_delivery_is_the_reference_result(client):
    evs = _events(client)
    for e in evs:
        client.post("/api/sync", json={"answers": [e]})
    st = _state(client)
    assert st["interval_hours"] == 75.0          # 24×2.5 → ×0.5 → ×2.5
    assert st["review_count"] == 3
    assert st["lapse_count"] == 1
    assert st["streak"] == 1
    assert st["next_review_at"] == "2026-07-23T05:00:00+00:00"  # t3 + 75h


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
    assert reference["interval_hours"] == 75.0


def test_out_of_order_arrival_rewrites_event_snapshots(client):
    """重算必須連事件的 before/after 快照一起改寫。

    regrade 靠 before 精確回捲上一次作答；快照沒跟著重算的話，那些數字指向
    一個從來不存在的狀態，「我是猜的」就會把卡片送到一個莫名其妙的間隔去。
    """
    evs = _events(client)
    client.post("/api/sync", json={"answers": [evs[2]]})  # 先送最新的
    client.post("/api/sync", json={"answers": [evs[0]]})  # 再送最舊的
    client.post("/api/sync", json={"answers": [evs[1]]})  # 中間那筆最後到

    assert _state(client)["interval_hours"] == 75.0

    # e3（t3，答對）現在是時間序最後一筆，但它是「第一個被寫進 DB 的」。
    # regrade 改判它為 forgot：正確的 before 是 e2 之後的狀態 interval=30，
    # 於是 30×0.5=15。若快照沒被重算，這裡會拿到當初那個 fresh→60 的舊值。
    r = client.post("/api/answer/regrade", json={"card_id": CARD}).json()
    assert r["interval_hours"] == 15.0
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


def test_batch_touching_one_card_many_times_rebuilds_once(client):
    """整批送達時亂序重算要延到迴圈外做，不能每碰一次算一次。

    這裡驗的是結果正確；效能面的保證由 rebuild 只在迴圈後跑一次來提供。
    """
    evs = _events(client)
    client.post("/api/sync", json={"answers": [evs[2]]})
    r = client.post("/api/sync", json={"answers": [evs[1], evs[0]]}).json()
    assert r["sync"]["applied"] == ["e1", "e2"]  # 批內仍照 reviewed_at 排序
    assert _state(client)["interval_hours"] == 75.0
