"""SQLite 存取層。只存「我對這張卡的記憶狀態」，題目內容不進 DB。

DB 檔放 repo 外（預設 ~/driver-license-data/app.db），git pull / reset 不會碰到進度。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .srs import DEFAULT_PRESET, CardState

DATA_DIR = Path(os.environ.get("DL_DATA_DIR", Path.home() / "driver-license-data"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS card (
  id                    TEXT PRIMARY KEY,
  deck                  TEXT NOT NULL,
  review_interval_hours REAL NOT NULL DEFAULT 24.0,
  next_review_at        TEXT,
  last_reviewed_at      TEXT,
  review_count          INTEGER NOT NULL DEFAULT 0,
  lapse_count           INTEGER NOT NULL DEFAULT 0,
  review_streak         INTEGER NOT NULL DEFAULT 0,
  last_review_feedback  INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS idx_card_queue ON card(deck, review_count, next_review_at);

CREATE TABLE IF NOT EXISTS review_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL,
  deck TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  day_key TEXT NOT NULL,
  chosen INTEGER,
  correct INTEGER NOT NULL,
  feedback INTEGER NOT NULL,
  elapsed_ms INTEGER,
  -- 完整的 before/after 快照：before 這半邊讓「我是猜的」能精確回捲上一次作答
  -- （streak 答錯會歸零，不存 before 就救不回來），也讓日後想改演算法可以整段回放。
  interval_before REAL, interval_after REAL,
  next_review_before TEXT, next_review_after TEXT,
  last_reviewed_before TEXT,
  review_count_before INTEGER, streak_before INTEGER, lapse_before INTEGER, feedback_before INTEGER,
  review_count_after INTEGER, streak_after INTEGER, lapse_after INTEGER
);
CREATE INDEX IF NOT EXISTS idx_event_day ON review_event(deck, day_key);
CREATE INDEX IF NOT EXISTS idx_event_card ON review_event(card_id);

CREATE TABLE IF NOT EXISTS note (
  card_id TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# 後加的欄位；比照 kg 的 _migrate_review_columns，用 idempotent ALTER 補齊舊 DB
_ADDED_COLUMNS = {
    "review_event": {
        "last_reviewed_before": "TEXT",
        "review_count_before": "INTEGER",
        "streak_before": "INTEGER",
        "lapse_before": "INTEGER",
        "feedback_before": "INTEGER",
    }
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or (DATA_DIR / "app.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def seed_cards(conn: sqlite3.Connection, cards: list[tuple[str, str]]) -> int:
    """upsert 補齊新題號、不刪舊卡（題庫改版時進度不會被清掉）。"""
    cur = conn.executemany(
        "INSERT INTO card(id, deck) VALUES(?, ?) ON CONFLICT(id) DO NOTHING", cards
    )
    conn.commit()
    return cur.rowcount


def get_state(conn: sqlite3.Connection, card_id: str) -> CardState | None:
    row = conn.execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        return None
    return CardState(
        review_interval_hours=row["review_interval_hours"],
        next_review_at=parse(row["next_review_at"]),
        last_reviewed_at=parse(row["last_reviewed_at"]),
        review_count=row["review_count"],
        lapse_count=row["lapse_count"],
        review_streak=row["review_streak"],
        last_review_feedback=row["last_review_feedback"],
    )


def put_state(conn: sqlite3.Connection, card_id: str, s: CardState) -> None:
    conn.execute(
        """UPDATE card SET review_interval_hours=?, next_review_at=?, last_reviewed_at=?,
                           review_count=?, lapse_count=?, review_streak=?, last_review_feedback=?
           WHERE id=?""",
        (
            s.review_interval_hours,
            iso(s.next_review_at),
            iso(s.last_reviewed_at),
            s.review_count,
            s.lapse_count,
            s.review_streak,
            s.last_review_feedback,
            card_id,
        ),
    )


def log_event(conn: sqlite3.Connection, **kw) -> None:
    cols = ",".join(kw)
    conn.execute(
        f"INSERT INTO review_event({cols}) VALUES({','.join('?' * len(kw))})",
        tuple(kw.values()),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def preset_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, "preset", DEFAULT_PRESET)


def reference_now(conn: sqlite3.Connection, now: datetime) -> datetime:
    """凍結時鐘（偷自 kg 的 reviewReferenceDate）：暫停期間到期計算改用 paused_at。

    出國/忙碌時不會累積出一座待複習山。
    """
    if get_setting(conn, "paused", "0") != "1":
        return now
    return parse(get_setting(conn, "paused_at", "")) or now
