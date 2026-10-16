"""可用性的上限：任何一個來源、任何一個帳號都不能拖垮其他人。

三個實測過的洞（2026-10-16）：
  1. 400 個偽造 IPv6 的假登入 → RSS 5.1 GB、匿名請求等 45 秒（限流以完整位址為 key，
     Argon2 並行數無上限）
  2. 任意 key 的筆記一次 22 MB、可無限重複；作答／重置／壓縮日誌無上限
  3. 每次壓縮都整段重算全部卡片 → 單一請求握寫鎖 8.7 秒，別的帳號的作答回 500
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import ratelimit

from tests.conftest import TEST_PASSWORD, TEST_USER

T0 = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)


# ---------------------------------------------------------------- IP 維度


@pytest.mark.parametrize("a, b, same", [
    ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", True),   # 同一個 /64
    ("2001:db8:1:2::1", "2001:db8:1:3::1", False),       # 隔壁的 /64
    ("::ffff:203.0.113.7", "203.0.113.7", True),          # IPv4-mapped 還原
    ("203.0.113.7", "203.0.113.8", False),
])
def test_ipv6_is_rate_limited_per_64(a, b, same):
    assert (ratelimit.ip_key(a) == ratelimit.ip_key(b)) is same


def test_unparseable_ip_is_kept_as_is():
    assert ratelimit.ip_key("unknown") == "unknown"


def test_rotating_addresses_inside_one_64_still_hits_the_limit(anon_client, monkeypatch):
    """TestClient 的 peer 是 "testclient"，把它當成受信任代理才走得到標頭那條路。"""
    monkeypatch.setattr(ratelimit, "TRUSTED_PROXIES", frozenset({"testclient"}))
    ratelimit.reset_all()
    codes = [
        anon_client.post(
            "/api/auth/login",
            json={"username": f"nobody{i}", "password": "wrong-but-long-enough"},
            headers={"CF-Connecting-IP": f"2001:db8:1:2::{i:x}"},
        ).status_code
        for i in range(30)
    ]
    assert 429 in codes, "換個 /64 內的位址就拿到新配額"


# ---------------------------------------------------------------- Argon2 並行


def test_argon2_gate_bounds_concurrency():
    from app.auth import _Argon2Gate

    gate = _Argon2Gate(slots=2, queue=100, wait_seconds=5)
    inside, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal inside, peak
        with gate.slot():
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


def test_a_saturated_gate_answers_503_instead_of_queueing(client, monkeypatch):
    import app.auth
    from app.auth import _Argon2Gate

    gate = _Argon2Gate(slots=1, queue=0, wait_seconds=0.05)
    monkeypatch.setattr(app.auth, "ARGON2_GATE", gate)
    entered, release = threading.Event(), threading.Event()

    def hog():
        with gate.slot():
            entered.set()
            release.wait(5)

    t = threading.Thread(target=hog)
    t.start()
    try:
        entered.wait(5)
        ratelimit.reset_all()
        r = client.post("/api/auth/login",
                        json={"username": TEST_USER, "password": TEST_PASSWORD})
        assert r.status_code == 503
        assert r.headers["retry-after"]
    finally:
        release.set()
        t.join()
    ratelimit.reset_all()
    assert client.post("/api/auth/login",
                       json={"username": TEST_USER, "password": TEST_PASSWORD}).status_code == 200


# ---------------------------------------------------------------- 筆記


def test_notes_only_attach_to_real_questions(client):
    assert client.put("/api/note/not-a-card", json={"body": "x"}).status_code == 404
    assert client.put("/api/note/C0001", json={"body": "ok"}).status_code == 200

    r = client.post("/api/sync", json={"notes": {"junk-1": "A" * 4000, "C0002": "kept"}})
    assert r.status_code == 200
    assert r.json()["sync"]["notes_failed"] == ["junk-1"]
    assert r.json()["notes"] == {"C0001": "ok", "C0002": "kept"}


def test_oversized_note_key_is_refused(client):
    r = client.post("/api/sync", json={"notes": {"x" * 5000: "a"}})
    assert r.status_code == 422


# ---------------------------------------------------------------- 日誌上限


def _answers(ids, prefix="e"):
    return [
        {"card_id": cid, "chosen": 1, "client_event_id": f"{prefix}{i}",
         "reviewed_at": (T0 + timedelta(minutes=i)).isoformat()}
        for i, cid in enumerate(ids)
    ]


def _event_count(client):
    import app.db
    with app.db.session() as conn:
        return app.db.log_counts(conn, 1)[0]


def test_daily_log_budget_rejects_the_whole_batch(client, monkeypatch):
    """整批 429、一列不寫：前端會把 failed 的作答丟掉，所以不能逐筆失敗。"""
    monkeypatch.setattr(ratelimit, "LOG_ROWS_USER", ratelimit.Limiter(capacity=3, per_seconds=86_400))
    ok = client.post("/api/sync", json={"answers": _answers(["C0001", "C0002", "C0003"])})
    assert ok.status_code == 200
    # 重送不算新列，不扣配額
    again = client.post("/api/sync", json={"answers": _answers(["C0001", "C0002", "C0003"])})
    assert again.status_code == 200 and len(again.json()["sync"]["skipped"]) == 3

    over = client.post("/api/sync", json={"answers": _answers(["C0004"], prefix="new")})
    assert over.status_code == 429
    assert _event_count(client) == 3
    assert client.post("/api/answer", json={"card_id": "C0005", "chosen": 1}).status_code == 429


def test_event_log_has_a_hard_cap(client, monkeypatch):
    import app.db
    monkeypatch.setattr(app.db, "MAX_EVENTS_PER_USER", 2)
    assert client.post("/api/sync", json={"answers": _answers(["C0001", "C0002"])}).status_code == 200
    r = client.post("/api/sync", json={"answers": _answers(["C0003"], prefix="x")})
    assert r.status_code == 409
    assert _event_count(client) == 2


def test_adjustments_past_the_cap_fail_without_blocking_the_sync(client, monkeypatch):
    import app.db
    monkeypatch.setattr(app.db, "MAX_ADJUSTMENTS_PER_USER", 2)
    adjustments = [{"client_action_id": f"c{i}", "factor": 1.5} for i in range(3)]
    r = client.post("/api/sync", json={
        "answers": _answers(["C0001"]), "adjustments": adjustments,
    })
    assert r.status_code == 200
    sync = r.json()["sync"]
    assert sync["adjustments"]["applied"] == ["c0", "c1"]
    assert [f["client_action_id"] for f in sync["adjustments"]["failed"]] == ["c2"]
    assert sync["applied"] == ["e0"]  # 作答照常寫入
    # 每次同步都重送全部 —— 已存在的那兩筆要被跳過，不能因為上限而失敗
    again = client.post("/api/sync", json={"adjustments": adjustments[:2]}).json()["sync"]
    assert again["adjustments"]["skipped"] == ["c0", "c1"]
    assert client.post("/api/progress/compress",
                       json={"client_action_id": "c9", "factor": 2}).status_code == 409


def test_resets_past_the_cap_fail(client, monkeypatch):
    import app.db
    monkeypatch.setattr(app.db, "MAX_RESETS_PER_USER", 1)
    assert client.post("/api/progress/reset",
                       json={"card_id": "C0001", "client_action_id": "r1"}).status_code == 200
    assert client.post("/api/progress/reset",
                       json={"card_id": "C0002", "client_action_id": "r2"}).status_code == 409


def test_per_request_caps_leave_room_above_the_per_account_caps():
    """前端每次同步都重送全部壓縮與重置。請求上限若不比帳號上限寬，
    碰到帳號上限的人每次同步都會 422，outbox 永遠送不出去。"""
    import app.db
    import app.main
    fields = app.main.SyncIn.model_fields
    caps = {m.max_length for m in fields["adjustments"].metadata if hasattr(m, "max_length")}
    assert min(caps) > app.db.MAX_ADJUSTMENTS_PER_USER
    caps = {m.max_length for m in fields["resets"].metadata if hasattr(m, "max_length")}
    assert min(caps) > app.db.MAX_RESETS_PER_USER


def test_absurd_elapsed_ms_is_clamped_not_a_500(client):
    r = client.post("/api/answer", json={"card_id": "C0001", "chosen": 1, "elapsed_ms": 2**70})
    assert r.status_code == 200


# ---------------------------------------------------------------- 壓縮重算成本


def _history(client):
    """幾張卡、亂序、含重置與一次中段壓縮 —— 讓等價性比對有東西可比。"""
    events = []
    for n, cid in enumerate(["C0001", "C0002", "C0003", "C0004"]):
        for k in range(3):
            events.append({
                "card_id": cid, "chosen": 1 + (n + k) % 3,
                "client_event_id": f"h{n}-{k}",
                "reviewed_at": (T0 + timedelta(hours=7 * k + n)).isoformat(),
            })
    client.post("/api/sync", json={
        "answers": events[::-1],
        "adjustments": [{"client_action_id": "mid", "factor": 1.7,
                         "applied_at": (T0 + timedelta(hours=8)).isoformat()}],
        "resets": [{"card_id": "C0004", "client_action_id": "r",
                    "reset_at": (T0 + timedelta(hours=10)).isoformat()}],
    })


def _states():
    import app.db
    with app.db.session() as conn:
        return sorted(app.db.all_states(conn, 1), key=lambda s: s["id"])


def test_tail_compression_skips_the_replay_and_matches_it(client, monkeypatch):
    import app.db
    _history(client)

    calls = {"n": 0}
    real = app.db.rebuild_card

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(app.db, "rebuild_card", counting)
    r = client.post("/api/progress/compress", json={
        "client_action_id": "tail", "factor": 2.3,
        "applied_at": (T0 + timedelta(days=30)).isoformat(),
    })
    assert r.status_code == 200
    assert calls["n"] == 0, "排在尾端的壓縮不該觸發整段重算"
    incremental = _states()

    # 對照組：把每張卡從日誌整段重算，結果必須一模一樣
    with app.db.session(write=True) as conn:
        app.db.rebuild_cards(conn, 1, app.db.card_keys(conn, 1), app.db.user_settings(conn, 1))
    assert _states() == incremental


def test_a_compression_inside_the_history_still_replays(client, monkeypatch):
    import app.db
    _history(client)
    calls = {"n": 0}
    real = app.db.rebuild_card

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(app.db, "rebuild_card", counting)
    client.post("/api/progress/compress", json={
        "client_action_id": "past", "factor": 2,
        "applied_at": (T0 + timedelta(hours=1)).isoformat(),
    })
    assert calls["n"] > 0


def test_a_batch_of_tail_compressions_matches_a_full_replay(client):
    """一批裡的尾端壓縮是最後一次套用的，中間又夾著作答：結果仍要等於整段重算。"""
    import app.db
    _history(client)
    late = T0 + timedelta(days=20)
    r = client.post("/api/sync", json={
        "adjustments": [
            {"client_action_id": f"b{i}", "factor": 1.3 + i / 10,
             "applied_at": (late + timedelta(hours=i)).isoformat()}
            for i in range(5)
        ],
        # C0001 在兩次壓縮之間作答，C0005 是新卡、晚於全部壓縮
        "answers": [
            {"card_id": "C0001", "chosen": 2, "client_event_id": "mid-batch",
             "reviewed_at": (late + timedelta(hours=2, minutes=30)).isoformat()},
            {"card_id": "C0005", "chosen": 1, "client_event_id": "after-batch",
             "reviewed_at": (late + timedelta(days=1)).isoformat()},
        ],
    })
    assert r.status_code == 200
    batched = _states()
    with app.db.session(write=True) as conn:
        app.db.rebuild_cards(conn, 1, app.db.card_keys(conn, 1), app.db.user_settings(conn, 1))
    assert _states() == batched
