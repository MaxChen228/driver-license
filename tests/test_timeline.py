"""統計圖共用時間序列模型的測試。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.timeline import build_timeline, get_scale


NOW = datetime(2026, 9, 10, 12, 34, 56, tzinfo=timezone.utc)


def test_past_and_future_share_the_same_minute_bucket_model():
    result = build_timeline(
        NOW,
        "minute",
        [
            (datetime(2026, 9, 10, 12, 34, 10, tzinfo=timezone.utc), True),
            (datetime(2026, 9, 10, 12, 33, 5, tzinfo=timezone.utc), False),
        ],
        [
            (NOW - timedelta(minutes=3), None),
            (NOW + timedelta(minutes=3), None),
        ],
    )

    assert result["scale"] == "minute"
    assert len(result["past"]) == len(result["future"]) == 60
    assert result["past"][-1] == {
        "at": "2026-09-10T12:34:00+00:00", "count": 1, "correct": 1,
    }
    assert result["past"][-2] == {
        "at": "2026-09-10T12:33:00+00:00", "count": 1, "correct": 0,
    }
    assert result["future"][0]["count"] == 1  # 已到期集中在目前這一分鐘
    assert result["future"][3]["count"] == 1


def test_unknown_timeline_scale_is_rejected():
    with pytest.raises(ValueError, match="unknown timeline scale"):
        get_scale("week")
