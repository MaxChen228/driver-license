"use strict";
// app/srs.py 的逐行對譯。存在的唯一理由是「離線時也要能立刻看到下次複習時間」——
// 沒網路時前端得自己算，回線後由伺服器重放同一批事件得出同樣的結果。
//
// 兩邊必須永遠一致。tests/test_srs_parity.py 會把這個檔剖出來、拿同一組輸入
// 跟 Python 版逐點比對，改了任一邊而沒改另一邊就會紅。

const FORGOT = 0;
const REMEMBERED = 1;

const UNLEARNED = "unlearned";
const DUE = "due";
const REVIEWED = "reviewed";

// 參數在本地，不從伺服器拿。匿名模式根本沒有伺服器可問，而讓同一組數字有兩個
// 來源只會製造「兩邊不一致」這種查不出來的 bug。與 app/srs.py 的 PRESETS 逐值
// 對應，tests/test_srs_parity.py 會比對兩邊完全相等。
const PRESETS = {
  relaxed: {
    initial_hours: 24.0, remembered_mult: 2.5, forgot_mult: 0.5,
    min_hours: 6.0, max_hours: 24 * 60,
  },
  intensive: {
    initial_hours: 8.0, remembered_mult: 1.4, forgot_mult: 0.35,
    min_hours: 4.0, max_hours: 24 * 60,
  },
};
const DEFAULT_PRESET = "relaxed";

function params(preset) {
  const cfg = PRESETS[preset];
  if (!cfg) throw new Error(`unknown preset: ${preset}`);
  return cfg;
}

// 夾擠順序有意義：先 base = max(current, min) 再乘、再夾回 [min, max]。
// 在下限的卡答錯是 6 × 0.5 = 3 → 夾回 6h，不是 3h。
function nextIntervalHours(currentHours, remembered, cfg) {
  const base = Math.max(currentHours, cfg.min_hours);
  const mult = remembered ? cfg.remembered_mult : cfg.forgot_mult;
  return Math.min(cfg.max_hours, Math.max(cfg.min_hours, base * mult));
}

// 狀態是算出來的、不存欄位（照 kg）。少一欄就少一種不同步的可能。
function deriveState(reviewCount, nextReviewAt, now) {
  if (!reviewCount) return UNLEARNED;
  if (!nextReviewAt || new Date(nextReviewAt) <= now) return DUE;
  return REVIEWED;
}

// 套用一次作答，回傳新狀態。純函式。
// 首次複習用 initial 當 base（忽略已存的 interval）；下次到期從「作答當下」起算。
function applyFeedback(state, remembered, cfg, now) {
  const s = state || {
    interval_hours: cfg.initial_hours, review_count: 0,
    lapse_count: 0, streak: 0, last_feedback: -1,
  };
  const base = s.review_count === 0 ? cfg.initial_hours : s.interval_hours;
  const interval = nextIntervalHours(base, remembered, cfg);
  return {
    interval_hours: interval,
    next_review_at: new Date(now.getTime() + interval * 3600e3).toISOString(),
    last_reviewed_at: now.toISOString(),
    review_count: s.review_count + 1,
    lapse_count: s.lapse_count + (remembered ? 0 : 1),
    streak: remembered ? s.streak + 1 : 0,
    last_feedback: remembered ? REMEMBERED : FORGOT,
  };
}

export {
  FORGOT, REMEMBERED, UNLEARNED, DUE, REVIEWED,
  PRESETS, DEFAULT_PRESET, params,
  nextIntervalHours, deriveState, applyFeedback,
};
