"""static/srs.js 必須跟 app/srs.py 算出完全一樣的結果。

離線作答由前端立刻算出下次複習時間，回線後伺服器重放同一批事件。兩邊只要有一點
不一致，使用者就會看到「答完顯示 60 小時、同步完變成別的數字」，而且不會有任何
錯誤訊息。這個測試把兩份實作丟同一組輸入逐點比對，改了一邊沒改另一邊就會紅。

沒有 node 就跳過（felix 上沒裝也不影響部署，這是開發期的護欄）。
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import srs

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="沒有 node，跳過 JS 對照")

# 涵蓋 srs.py docstring 裡釘死的每個分支
CASES = [
    # (interval_hours, review_count, remembered, preset)
    (24.0, 0, True, "relaxed"),    # 首答答對：用 initial 當 base
    (24.0, 0, False, "relaxed"),   # 首答答錯
    (999.0, 0, True, "relaxed"),   # 首次複習忽略已存的 interval
    (60.0, 1, True, "relaxed"),
    (720.0, 5, False, "relaxed"),  # 答錯是 ×0.5，不是歸零
    (6.0, 3, False, "relaxed"),    # 在下限答錯：6×0.5=3 → 夾回 6
    (1.0, 3, True, "relaxed"),     # 低於下限先抬到 min 再乘
    (1000.0, 9, True, "relaxed"),  # 撞上限 1440
    (8.0, 0, True, "intensive"),
    (8.0, 0, False, "intensive"),
    (4.0, 2, False, "intensive"),  # intensive 下限
]

HARNESS = """
import * as srs from "./srs.mjs";
const cases = JSON.parse(process.argv[2]);
const presets = JSON.parse(process.argv[3]);
const now = new Date(process.argv[4]);
const out = cases.map(([interval, count, remembered, preset]) => {
  const s = { interval_hours: interval, review_count: count,
              lapse_count: 0, streak: 0, last_feedback: -1 };
  const a = srs.applyFeedback(s, remembered, presets[preset], now);
  return [a.interval_hours, a.next_review_at, a.review_count, a.lapse_count,
          a.streak, a.last_feedback];
});
process.stdout.write(JSON.stringify(out));
"""


def test_js_and_python_schedulers_agree(tmp_path):
    # 複製成 .mjs：node 靠副檔名判斷模組型別，.js 會被當成 CommonJS 而在 export 炸掉。
    # 瀏覽器不看副檔名（<script type="module"> + 明確的 import 路徑），所以不影響線上。
    shutil.copy(ROOT / "static" / "srs.js", tmp_path / "srs.mjs")
    (tmp_path / "harness.mjs").write_text(HARNESS, encoding="utf-8")

    now = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
    presets = {
        name: {
            "initial_hours": c.initial_hours, "remembered_mult": c.remembered_mult,
            "forgot_mult": c.forgot_mult, "min_hours": c.min_hours, "max_hours": c.max_hours,
        }
        for name, c in srs.PRESETS.items()
    }

    proc = subprocess.run(
        [NODE, str(tmp_path / "harness.mjs"), json.dumps(CASES), json.dumps(presets),
         now.isoformat()],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    )
    js = json.loads(proc.stdout)

    py = []
    for interval, count, remembered, preset in CASES:
        cfg = srs.PRESETS[preset]
        state = srs.CardState(interval, None, None, count, 0, 0, -1)
        a = srs.apply_feedback(state, remembered, cfg, now)
        py.append([
            a.review_interval_hours, a.next_review_at,
            a.review_count, a.lapse_count, a.review_streak, a.last_review_feedback,
        ])

    for case, j, p in zip(CASES, js, py):
        # 到期時間比 datetime 不比字串：兩邊的 ISO 格式不同（JS 帶毫秒與 Z，
        # Python 帶 +00:00），字串比對會在某個小數間隔的案例上假性失敗。
        j_at = datetime.fromisoformat(j[1].replace("Z", "+00:00"))
        assert [j[0], j_at, *j[2:]] == p, f"{case} 不一致：js={j} py={p}"


def test_js_and_python_derive_state_agree(tmp_path):
    shutil.copy(ROOT / "static" / "srs.js", tmp_path / "srs.mjs")
    (tmp_path / "h.mjs").write_text(
        """
        import * as srs from "./srs.mjs";
        const cases = JSON.parse(process.argv[2]);
        const now = new Date(process.argv[3]);
        process.stdout.write(JSON.stringify(
          cases.map(([c, nxt]) => srs.deriveState(c, nxt, now))));
        """,
        encoding="utf-8",
    )
    now = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
    cases = [
        (0, None),
        (0, "2026-07-19T02:00:00+00:00"),   # 沒複習過就是未學習，不管到期欄位
        (1, None),
        (1, "2026-07-19T02:00:00+00:00"),   # 已過期
        (1, "2026-07-20T02:00:00+00:00"),   # 剛好到期 → due（邊界含等號）
        (1, "2026-07-21T02:00:00+00:00"),
    ]
    proc = subprocess.run(
        [NODE, str(tmp_path / "h.mjs"), json.dumps(cases), now.isoformat()],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    )
    js = json.loads(proc.stdout)
    py = [
        srs.derive_state(c, datetime.fromisoformat(n) if n else None, now) for c, n in cases
    ]
    assert js == py


def test_presets_match_python(tmp_path):
    """匿名模式的 SRS 參數只能來自 static/srs.js —— 沒有伺服器可問。

    所以那份常數就是唯一來源，而它必須跟 app/srs.py 一個小數位都不差。
    差一點點不會壞掉，只會讓同一個人在登入前後拿到不同的複習時間。
    """
    harness = tmp_path / "presets.mjs"
    shutil.copy(ROOT / "static" / "srs.js", tmp_path / "srs.mjs")
    harness.write_text(
        "import * as srs from './srs.mjs';\n"
        "console.log(JSON.stringify({presets: srs.PRESETS, dflt: srs.DEFAULT_PRESET}));\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [NODE, str(harness)], capture_output=True, text=True, check=True, cwd=tmp_path
    )
    got = json.loads(out.stdout)

    assert got["dflt"] == srs.DEFAULT_PRESET
    assert set(got["presets"]) == set(srs.PRESETS)
    for name, cfg in srs.PRESETS.items():
        js = got["presets"][name]
        assert js == {
            "initial_hours": cfg.initial_hours,
            "remembered_mult": cfg.remembered_mult,
            "forgot_mult": cfg.forgot_mult,
            "min_hours": cfg.min_hours,
            "max_hours": cfg.max_hours,
        }, name
