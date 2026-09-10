"""統計圖共用的時間序列模型。

兩張圖都由固定長度的時間桶組成：past 放作答事件，future 放到期卡片。
分桶規則集中在這裡，避免「最近作答」與「未來到期」各自實作一份。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

TimelineDirection = Literal["past", "future"]


@dataclass(frozen=True)
class TimelineScale:
    key: str
    bucket: timedelta
    count: int


SCALES = {
    "day": TimelineScale("day", timedelta(days=1), 14),
    "hour": TimelineScale("hour", timedelta(hours=1), 24),
    "minute": TimelineScale("minute", timedelta(minutes=1), 60),
}


def get_scale(key: str) -> TimelineScale:
    try:
        return SCALES[key]
    except KeyError:
        raise ValueError(f"unknown timeline scale: {key}") from None


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _floor(dt: datetime, scale: TimelineScale) -> datetime:
    seconds = int(scale.bucket.total_seconds())
    timestamp = int(_utc(dt).timestamp())
    return datetime.fromtimestamp(timestamp - timestamp % seconds, timezone.utc)


def _empty(now: datetime, scale: TimelineScale, direction: TimelineDirection) -> list[dict]:
    anchor = _floor(now, scale)
    origin = anchor if direction == "future" else anchor - scale.bucket * (scale.count - 1)
    return [
        {
            "at": (origin + scale.bucket * i).isoformat(),
            "count": 0,
            "correct": 0,
        }
        for i in range(scale.count)
    ]


def _bucket_items(
    items: Iterable[tuple[datetime | None, bool | None]],
    now: datetime,
    scale: TimelineScale,
    direction: TimelineDirection,
) -> list[dict]:
    rows = _empty(now, scale, direction)
    now = _utc(now)
    anchor = _floor(now, scale)
    origin = anchor if direction == "future" else anchor - scale.bucket * (scale.count - 1)

    for at, correct in items:
        if at is None:
            continue
        at = _utc(at)
        if direction == "past" and at > now:
            continue

        bucket = _floor(at, scale)
        index = (bucket - origin) // scale.bucket
        if direction == "future" and index < 0:
            # 已經到期的卡集中到目前這一桶，與日尺度的既有行為一致。
            index = 0
        if not 0 <= index < scale.count:
            continue

        rows[index]["count"] += 1
        if correct is True:
            rows[index]["correct"] += 1
    return rows


def build_timeline(
    now: datetime,
    scale_key: str,
    activity: Iterable[tuple[datetime | None, bool | None]],
    due: Iterable[tuple[datetime | None, bool | None]],
) -> dict:
    """用同一套時間桶建立作答與到期兩條序列。"""
    scale = get_scale(scale_key)
    return {
        "scale": scale.key,
        "past": _bucket_items(activity, now, scale, "past"),
        "future": _bucket_items(due, now, scale, "future"),
    }
