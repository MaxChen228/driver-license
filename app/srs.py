"""間隔重複排程 —— 移植自 ~/project/kg 的 VocabularyReviewPolicy。

刻意「不是」SM-2 / Leitner / FSRS：二元評分、乘法式間隔、單位小時、無 ease factor。

三個載重細節（kg 的 VocabularyReviewPolicyTests 把每個分支都釘死，別改）：
  1. 答錯是 interval × forgot_mult，不是歸零重來。排到 30 天的卡答錯回到 15 天，
     不會被打回新卡 —— 這跟 Anki 的 relearning queue 是不同哲學。
  2. 夾擠順序：先 base = max(current, min) 再乘、再夾回 [min, max]。
     所以在下限的卡答錯是 6 × 0.5 = 3 → 夾回 6h，不是 3h。
  3. 首次複習用 initial 當 base（忽略已存的 interval）；下次到期從「作答當下」
     起算，不是從原到期日起算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FORGOT = 0
REMEMBERED = 1


@dataclass(frozen=True)
class ReviewSettings:
    initial_hours: float
    remembered_mult: float
    forgot_mult: float
    min_hours: float
    max_hours: float

    @staticmethod
    def preset(name: str) -> "ReviewSettings":
        try:
            return PRESETS[name]
        except KeyError:
            raise ValueError(f"unknown preset: {name}") from None


# kg 的兩組 preset，數值原樣搬過來
PRESETS: dict[str, ReviewSettings] = {
    "relaxed": ReviewSettings(24.0, 2.5, 0.5, 6.0, 24 * 60),
    "intensive": ReviewSettings(8.0, 1.4, 0.35, 4.0, 24 * 60),
}
DEFAULT_PRESET = "relaxed"


def next_interval_hours(current_hours: float, remembered: bool, cfg: ReviewSettings) -> float:
    """kg VocabularyReviewPolicy.nextIntervalHours 的直譯。夾擠順序見模組 docstring。"""
    base = max(current_hours, cfg.min_hours)
    mult = cfg.remembered_mult if remembered else cfg.forgot_mult
    return min(cfg.max_hours, max(cfg.min_hours, base * mult))


@dataclass(frozen=True)
class CardState:
    """卡片的複習狀態。注意沒有 `state` 欄 —— 狀態是 derive 出來的（見 derive_state）。"""

    review_interval_hours: float
    next_review_at: datetime | None
    last_reviewed_at: datetime | None
    review_count: int
    lapse_count: int
    review_streak: int
    last_review_feedback: int

    @staticmethod
    def fresh(cfg: ReviewSettings) -> "CardState":
        return CardState(cfg.initial_hours, None, None, 0, 0, 0, -1)


UNLEARNED = "unlearned"
DUE = "due"
REVIEWED = "reviewed"


def derive_state(review_count: int, next_review_at: datetime | None, now: datetime) -> str:
    """kg 的 reviewState(at:)：狀態是算出來的，不存欄位。少一欄就少一種不同步的可能。"""
    if review_count == 0:
        return UNLEARNED
    if next_review_at is None or next_review_at <= now:
        return DUE
    return REVIEWED


def apply_feedback(
    state: CardState, remembered: bool, cfg: ReviewSettings, now: datetime
) -> CardState:
    """套用一次作答，回傳新狀態。純函式，不碰 DB。"""
    base = cfg.initial_hours if state.review_count == 0 else state.review_interval_hours
    interval = next_interval_hours(base, remembered, cfg)
    return CardState(
        review_interval_hours=interval,
        next_review_at=now + timedelta(hours=interval),
        last_reviewed_at=now,
        review_count=state.review_count + 1,
        lapse_count=state.lapse_count + (0 if remembered else 1),
        review_streak=state.review_streak + 1 if remembered else 0,
        last_review_feedback=REMEMBERED if remembered else FORGOT,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
