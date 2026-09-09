"""比照 kg 的 VocabularyReviewPolicyTests，把每個分支釘死。

這些數字不是隨便選的 —— 改動任何一個都代表排程哲學被改了，要先確認是有意的。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.srs import (
    DUE,
    FORGOT,
    PRESETS,
    REMEMBERED,
    REVIEWED,
    UNLEARNED,
    CardState,
    ReviewSettings,
    apply_feedback,
    compress_state,
    derive_state,
    next_interval_hours,
)

RELAXED = PRESETS["relaxed"]
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def test_relaxed_preset_matches_kg():
    assert (RELAXED.initial_hours, RELAXED.remembered_mult, RELAXED.forgot_mult) == (24.0, 2.5, 0.5)
    assert (RELAXED.min_hours, RELAXED.max_hours) == (6.0, 1440.0)


def test_intensive_preset_matches_kg():
    c = PRESETS["intensive"]
    assert (c.initial_hours, c.remembered_mult, c.forgot_mult, c.min_hours, c.max_hours) == (
        8.0, 1.4, 0.35, 4.0, 1440.0,
    )


def test_remembered_multiplies():
    assert next_interval_hours(24, True, RELAXED) == 60.0


def test_forgot_shrinks_but_does_not_reset():
    """答錯是 ×0.5，不是歸零 —— 這是 kg 與 Anki relearning queue 的哲學分歧點。"""
    assert next_interval_hours(720, False, RELAXED) == 360.0


def test_forgot_at_floor_clamps_back_to_min():
    """夾擠順序載重：6 × 0.5 = 3 → 夾回 6，不是 3。"""
    assert next_interval_hours(6, False, RELAXED) == 6.0


def test_below_floor_is_raised_before_multiplying():
    """base = max(current, min) 先發生：1h 的卡答對是 6×2.5=15，不是 1×2.5=2.5。"""
    assert next_interval_hours(1, True, RELAXED) == 15.0


def test_max_is_capped_at_60_days():
    assert next_interval_hours(1000, True, RELAXED) == 1440.0
    assert next_interval_hours(1440, True, RELAXED) == 1440.0


def test_first_review_uses_initial_as_base_ignoring_stored_interval():
    s = CardState(review_interval_hours=999.0, next_review_at=None, last_reviewed_at=None,
                  review_count=0, lapse_count=0, review_streak=0, last_review_feedback=-1)
    after = apply_feedback(s, True, RELAXED, NOW)
    assert after.review_interval_hours == 168.0  # 第一次就答對：直接排到 7 天後


def test_next_review_is_measured_from_answer_time():
    s = CardState.fresh(RELAXED)
    after = apply_feedback(s, True, RELAXED, NOW)
    assert after.next_review_at == NOW + timedelta(hours=168)


def test_compress_state_halves_interval_without_erasing_history():
    state = CardState(
        review_interval_hours=420.0,
        next_review_at=NOW + timedelta(days=10),
        last_reviewed_at=NOW,
        review_count=2,
        lapse_count=1,
        review_streak=1,
        last_review_feedback=REMEMBERED,
    )
    compressed = compress_state(state, RELAXED)
    assert compressed.review_interval_hours == 210.0
    assert compressed.next_review_at == NOW + timedelta(hours=210)
    assert (compressed.review_count, compressed.lapse_count, compressed.review_streak) == (2, 1, 1)


def test_streak_and_lapse_transitions():
    s = CardState.fresh(RELAXED)
    s = apply_feedback(s, True, RELAXED, NOW)
    s = apply_feedback(s, True, RELAXED, NOW)
    assert (s.review_streak, s.lapse_count, s.review_count) == (2, 0, 2)
    assert s.last_review_feedback == REMEMBERED

    s = apply_feedback(s, False, RELAXED, NOW)
    assert (s.review_streak, s.lapse_count, s.review_count) == (0, 1, 3)
    assert s.last_review_feedback == FORGOT


def test_forgot_keeps_accumulated_strength():
    """連續答對排到 1050h 的卡答錯後回到 525h，不會被打回新卡的 24h。"""
    s = CardState.fresh(RELAXED)
    for _ in range(3):
        s = apply_feedback(s, True, RELAXED, NOW)
    assert s.review_interval_hours == 1050.0  # 168→420→1050
    s = apply_feedback(s, False, RELAXED, NOW)
    assert s.review_interval_hours == 525.0
    assert s.review_count == 4  # 仍是複習卡，不是新卡


@pytest.mark.parametrize(
    "count,nxt,expected",
    [
        (0, None, UNLEARNED),
        (0, NOW - timedelta(hours=1), UNLEARNED),  # 沒答過就是未學習，不看 due
        (1, NOW - timedelta(hours=1), DUE),
        (1, NOW, DUE),
        (1, NOW + timedelta(hours=1), REVIEWED),
    ],
)
def test_derive_state(count, nxt, expected):
    assert derive_state(count, nxt, NOW) == expected


def test_unknown_preset_rejected():
    with pytest.raises(ValueError):
        ReviewSettings.preset("nope")
