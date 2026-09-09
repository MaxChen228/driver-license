"""複習進度壓縮：調整本身也要能跨裝置同步並在事件重算後保留。"""

from datetime import datetime, timedelta, timezone


def _answer(client, card_id, at):
    question = client.get(f"/api/card/{card_id}").json()["question"]
    return client.post(
        "/api/answer",
        json={
            "card_id": card_id,
            "chosen": question["answer"],
            "reviewed_at": at.isoformat(),
        },
    ).json()


def _state(client, card_id):
    return next(s for s in client.get("/api/state").json()["states"] if s["id"] == card_id)


def test_compress_halves_existing_schedule_and_is_idempotent(client):
    card_id = "C0001"
    t0 = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    _answer(client, card_id, t0)
    _answer(client, card_id, t0 + timedelta(days=1))
    assert _state(client, card_id)["interval_hours"] == 420.0

    action = {
        "client_action_id": "compress-1",
        "applied_at": (t0 + timedelta(days=2)).isoformat(),
        "factor": 2,
    }
    first = client.post("/api/progress/compress", json=action).json()
    state = next(s for s in first["states"] if s["id"] == card_id)
    assert state["interval_hours"] == 210.0
    assert state["next_review_at"] == "2026-07-29T20:00:00+00:00"
    assert state["review_count"] == 2 and state["lapse_count"] == 0
    assert first["adjustments"][0]["client_action_id"] == "compress-1"

    again = client.post("/api/progress/compress", json=action).json()
    assert next(s for s in again["states"] if s["id"] == card_id) == state
    assert len(again["adjustments"]) == 1


def test_compression_survives_a_late_answer_rebuild(client):
    card_id = "C0002"
    t0 = datetime(2026, 7, 20, 2, tzinfo=timezone.utc)
    _answer(client, card_id, t0)
    client.post("/api/progress/compress", json={
        "client_action_id": "compress-late",
        "applied_at": (t0 + timedelta(days=2)).isoformat(),
        "factor": 2,
    })

    # 這筆事件在壓縮之前發生，卻在壓縮之後才抵達；重算後仍要保留壓縮。
    _answer(client, card_id, t0 + timedelta(days=1))
    state = _state(client, card_id)
    assert state["review_count"] == 2
    assert state["interval_hours"] == 210.0
    assert state["next_review_at"] == "2026-07-29T20:00:00+00:00"


def test_compress_does_not_create_progress_for_unlearned_cards(client):
    before = client.get("/api/state").json()
    result = client.post("/api/progress/compress", json={
        "client_action_id": "compress-empty",
        "applied_at": "2026-07-20T02:00:00+00:00",
        "factor": 2,
    }).json()
    assert result["states"] == []
    assert result["adjustments"]
    assert before["states"] == []
