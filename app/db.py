"""SQLite 存取層。只存「我對這張卡的記憶狀態」，題目內容不進 DB。

DB 檔放 repo 外（預設 ~/driver-license-data/app.db），git pull / reset 不會碰到進度。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .srs import DEFAULT_PRESET, CardState, ReviewSettings

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
        # 離線作答補送用：前端產生的 uuid。SQLite 的 UNIQUE 索引允許多個 NULL，
        # 所以線上作答留白不會互撞，補送重覆送則會被索引擋下（見 log_event）。
        "client_event_id": "TEXT",
    }
}

_ADDED_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_client ON review_event(client_event_id)",
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    for stmt in _ADDED_INDEXES:
        conn.execute(stmt)


def db_path() -> Path:
    return DATA_DIR / "app.db"


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # 沒有 busy_timeout 的話，第二個寫入者會立刻拿到 SQLITE_BUSY 而不是等一下。
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下的建議值：省掉每次 commit 的 fsync


def init(path: Path | None = None) -> None:
    """建檔、建 schema、跑遷移。程序啟動時呼叫一次。"""
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        _configure(conn)
        conn.execute("PRAGMA journal_mode=WAL")  # 寫進檔案本身，設一次就永久有效
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(write: bool = False):
    """一個請求一條連線，交易邊界明確。

    原本是「整個 process 共用一條連線 + 一把 Python threading.Lock」。那把鎖擋得住
    同 process 的交錯 COMMIT，但擋不住多 worker，而且兩條寫入路徑（PUT /api/note、
    PUT /api/settings）根本沒進鎖。改成交給 SQLite 自己：寫入走 BEGIN IMMEDIATE，
    開頭就取得 RESERVED 鎖，衝突時由 busy_timeout 等待而不是寫到一半才炸。

    讀取走 BEGIN DEFERRED，WAL 下等於拿到一個穩定快照 —— /api/state 要一次讀
    states + notes + settings，中間被寫入插隊會回傳自相矛盾的內容。
    """
    conn = sqlite3.connect(db_path(), isolation_level=None)  # 交易由我們自己開
    try:
        _configure(conn)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, card_id: str, cfg: ReviewSettings) -> CardState:
    """查無列 = 沒答過 = 全新卡。

    以前是啟動時把 1896 張卡全部 seed 進 DB，所以列必然存在。多使用者之後那筆
    帳變成「每註冊一人寫 1896 列」，不能接受；改成答過才建列。derive_state 本來
    就把 review_count == 0 定義成未學習，「沒有列」與「有列但沒答過」語義相同。
    """
    row = conn.execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        return CardState.fresh(cfg)
    return CardState(
        review_interval_hours=row["review_interval_hours"],
        next_review_at=parse(row["next_review_at"]),
        last_reviewed_at=parse(row["last_reviewed_at"]),
        review_count=row["review_count"],
        lapse_count=row["lapse_count"],
        review_streak=row["review_streak"],
        last_review_feedback=row["last_review_feedback"],
    )


def put_state(conn: sqlite3.Connection, card_id: str, deck: str, s: CardState) -> None:
    """upsert —— 惰性建列之後，第一次作答時該列還不存在。"""
    conn.execute(
        """INSERT INTO card(id, deck, review_interval_hours, next_review_at, last_reviewed_at,
                            review_count, lapse_count, review_streak, last_review_feedback)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             review_interval_hours=excluded.review_interval_hours,
             next_review_at=excluded.next_review_at,
             last_reviewed_at=excluded.last_reviewed_at,
             review_count=excluded.review_count,
             lapse_count=excluded.lapse_count,
             review_streak=excluded.review_streak,
             last_review_feedback=excluded.last_review_feedback""",
        (
            card_id,
            deck,
            s.review_interval_hours,
            iso(s.next_review_at),
            iso(s.last_reviewed_at),
            s.review_count,
            s.lapse_count,
            s.review_streak,
            s.last_review_feedback,
        ),
    )


def log_event(conn: sqlite3.Connection, **kw) -> None:
    cols = ",".join(kw)
    conn.execute(
        f"INSERT INTO review_event({cols}) VALUES({','.join('?' * len(kw))})",
        tuple(kw.values()),
    )


def event_exists(conn: sqlite3.Connection, client_event_id: str) -> bool:
    """離線補送的冪等閘門：同一個 client_event_id 只能算一次分。

    手機補送到一半斷線、使用者重新整理再送一次，是必然會發生的事；
    沒有這道閘門就會被當成兩次作答，間隔連乘兩次、review_count 多算。
    """
    return conn.execute(
        "SELECT 1 FROM review_event WHERE client_event_id=?", (client_event_id,)
    ).fetchone() is not None


def all_states(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "id": r["id"],
            "deck": r["deck"],
            "interval_hours": r["review_interval_hours"],
            "next_review_at": r["next_review_at"],
            "last_reviewed_at": r["last_reviewed_at"],
            "review_count": r["review_count"],
            "lapse_count": r["lapse_count"],
            "streak": r["review_streak"],
            "last_feedback": r["last_review_feedback"],
        }
        for r in conn.execute(
            """SELECT id, deck, review_interval_hours, next_review_at, last_reviewed_at,
                      review_count, lapse_count, review_streak, last_review_feedback
               FROM card"""
        )
    ]


def all_notes(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["card_id"]: r["body"] for r in conn.execute("SELECT card_id, body FROM note")}


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def preset_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, "preset", DEFAULT_PRESET)


def reference_now(conn: sqlite3.Connection, now: datetime) -> datetime:
    """凍結時鐘（偷自 kg 的 reviewReferenceDate）：暫停期間到期計算改用 paused_at。

    出國/忙碌時不會累積出一座待複習山。
    """
    if get_setting(conn, "paused", "0") != "1":
        return now
    return parse(get_setting(conn, "paused_at", "")) or now
