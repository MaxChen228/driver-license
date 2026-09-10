"""間隔重複排程 —— 移植自 ~/project/kg 的 VocabularyReviewPolicy。

刻意「不是」SM-2 / Leitner / FSRS：二元評分、乘法式間隔、單位小時、無 ease factor。

三個載重細節（kg 的 VocabularyReviewPolicyTests 把每個分支都釘死，別改）：
  1. 答錯是 interval × forgot_mult，不是歸零重來。排到 30 天的卡答錯回到 15 天，
     不會被打回新卡 —— 這跟 Anki 的 relearning queue 是不同哲學。
  2. 作答後只套倍率並限制上限，不設最低間隔；所以 8 × 0.1 = 0.8h。
  3. 首次複習用 initial 當 base（忽略已存的 interval）；下次到期從「作答當下」
     起算，不是從原到期日起算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FORGOT = 0
REMEMBERED = 1
FIRST_REMEMBERED_HOURS = 24 * 7


@dataclass(frozen=True)
class ReviewSettings:
    initial_hours: float
    remembered_mult: float
    forgot_mult: float
    # 保留於設定／事件格式，供舊資料相容；作答與壓縮不再使用最低值。
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

# 第三個選項：使用者自己調的倍率。它不在 PRESETS 裡，因為 PRESETS 是「名字 →
# 固定數值」的表，而 custom 的數值會變 —— 這個差別是整段設計的關鍵：
# review_event 存的是 preset 的**名字**，重算時再查表換回數值（見 db.rebuild_card
# 的 docstring）。如果 custom 也只存名字，那麼使用者每調一次倍率，過去所有事件
# 都會被用新數值重算一遍 —— 正是那份 docstring 花一整段在防的事。
# 所以 custom 的事件必須把「當時那組數值」一起存下來（review_event.preset_params）。
CUSTOM_PRESET = "custom"

# 可調範圍。兩個邊界都不是美觀問題，是「排程還成不成立」的問題：
#   答對倍率 ≤ 1 → 答對之後間隔不變或變短，佇列只會越長越大。
#   答錯倍率 ≥ 1 → 答錯之後間隔沒縮短，這個排程就沒有在回應「你忘了」。
# 上界純粹是防呆（×5 已經是「答對三次就排到上限」）。
REMEMBERED_MULT_RANGE = (1.05, 5.0)
FORGOT_MULT_RANGE = (0.001, 0.95)
MULT_DECIMAL_PLACES = 3


def custom_settings(base: ReviewSettings, remembered_mult: float, forgot_mult: float) -> ReviewSettings:
    """在 base（切到 custom 當下那組 preset）上換掉兩個倍率，其餘沿用。

    首次間隔與時間上限刻意不開放調整：這一版只做使用者要的那兩個旋鈕。完整參數
    仍然一起存下來，日後要開放其他項不必再動一次 schema。
    """
    return ReviewSettings(
        initial_hours=base.initial_hours,
        remembered_mult=clamp_mult(remembered_mult, REMEMBERED_MULT_RANGE),
        forgot_mult=clamp_mult(forgot_mult, FORGOT_MULT_RANGE),
        min_hours=base.min_hours,
        max_hours=base.max_hours,
    )


def clamp_mult(value: float, rng: tuple[float, float]) -> float:
    lo, hi = rng
    return round(min(hi, max(lo, float(value))), MULT_DECIMAL_PLACES)


def resolve(name: str, params: dict | None = None) -> ReviewSettings:
    """名字（＋ custom 才有的那組數值）→ 實際參數。

    params 只有在 name == custom 時才看；具名 preset 一律以表為準，免得舊事件
    帶著一份與名字對不起來的數值，重算出來的東西誰也解釋不了。
    """
    if name != CUSTOM_PRESET:
        return ReviewSettings.preset(name)
    p = params or {}
    base = PRESETS.get(str(p.get("base") or DEFAULT_PRESET), PRESETS[DEFAULT_PRESET])
    return ReviewSettings(
        initial_hours=float(p.get("initial_hours", base.initial_hours)),
        remembered_mult=clamp_mult(p.get("remembered_mult", base.remembered_mult), REMEMBERED_MULT_RANGE),
        forgot_mult=clamp_mult(p.get("forgot_mult", base.forgot_mult), FORGOT_MULT_RANGE),
        min_hours=float(p.get("min_hours", base.min_hours)),
        max_hours=float(p.get("max_hours", base.max_hours)),
    )


def as_params(cfg: ReviewSettings, base: str = DEFAULT_PRESET) -> dict:
    """ReviewSettings → 可以存進 user_setting／review_event 的 dict。"""
    return {
        "base": base,
        "initial_hours": cfg.initial_hours,
        "remembered_mult": cfg.remembered_mult,
        "forgot_mult": cfg.forgot_mult,
        "min_hours": cfg.min_hours,
        "max_hours": cfg.max_hours,
    }


def next_interval_hours(current_hours: float, remembered: bool, cfg: ReviewSettings) -> float:
    """套用一次倍率；只限制上限，讓答錯倍率能完整反映在間隔上。"""
    base = current_hours
    mult = cfg.remembered_mult if remembered else cfg.forgot_mult
    return min(cfg.max_hours, base * mult)


def compress_state(state: CardState, cfg: ReviewSettings, factor: float = 2.0) -> CardState:
    """把已建立的複習間隔按比例縮短，保留作答次數與錯題統計。"""
    if factor <= 0:
        raise ValueError("compression factor must be positive")
    if state.review_count == 0:
        return state
    interval = min(cfg.max_hours, state.review_interval_hours / factor)
    next_review_at = state.next_review_at
    if state.last_reviewed_at is not None:
        next_review_at = state.last_reviewed_at + timedelta(hours=interval)
    return CardState(
        review_interval_hours=interval,
        next_review_at=next_review_at,
        last_reviewed_at=state.last_reviewed_at,
        review_count=state.review_count,
        lapse_count=state.lapse_count,
        review_streak=state.review_streak,
        last_review_feedback=state.last_review_feedback,
    )


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
    interval = (
        min(cfg.max_hours, FIRST_REMEMBERED_HOURS)
        if state.review_count == 0 and remembered
        else next_interval_hours(base, remembered, cfg)
    )
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
