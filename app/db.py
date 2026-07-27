"""SQLite 存取層。只存「我對這張卡的記憶狀態」，題目內容不進 DB。

DB 檔放 repo 外（預設 ~/driver-license-data/app.db），git pull / reset 不會碰到進度。

每一列使用者資料都掛 user_id。沒有「全域的那一份進度」這種東西 —— 所有讀寫
一律指名擁有者，跨帳號讀到別人的資料在型別上就做不到（函式簽名逼你給 user_id）。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .srs import DEFAULT_PRESET, CardState, ReviewSettings

DATA_DIR = Path(os.environ.get("DL_DATA_DIR", Path.home() / "driver-license-data"))

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  -- NULL = 這個帳號還不能登入（遷移建出來的舊有進度，等 useradmin 設密碼認領）
  password_hash TEXT,
  recovery_hash TEXT,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS session (
  token_sha256 TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  last_used_at TEXT,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id);

CREATE TABLE IF NOT EXISTS card_state (
  user_id               INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  card_id               TEXT NOT NULL,
  deck                  TEXT NOT NULL,
  review_interval_hours REAL NOT NULL DEFAULT 24.0,
  next_review_at        TEXT,
  last_reviewed_at      TEXT,
  review_count          INTEGER NOT NULL DEFAULT 0,
  lapse_count           INTEGER NOT NULL DEFAULT 0,
  review_streak         INTEGER NOT NULL DEFAULT 0,
  last_review_feedback  INTEGER NOT NULL DEFAULT -1,
  PRIMARY KEY (user_id, card_id)
);
CREATE INDEX IF NOT EXISTS idx_card_queue
  ON card_state(user_id, deck, review_count, next_review_at);

CREATE TABLE IF NOT EXISTS review_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
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
  review_count_after INTEGER, streak_after INTEGER, lapse_after INTEGER,
  -- 離線補送用：前端產生的冪等鍵。SQLite 的 UNIQUE 索引允許多個 NULL，
  -- 所以線上作答留白不會互撞，補送重覆送則會被索引擋下（見 event_exists）。
  client_event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_day ON review_event(user_id, deck, day_key);
CREATE INDEX IF NOT EXISTS idx_event_card ON review_event(user_id, card_id, reviewed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_client
  ON review_event(user_id, client_event_id);

CREATE TABLE IF NOT EXISTS note (
  user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  card_id TEXT NOT NULL,
  body    TEXT NOT NULL,
  updated TEXT NOT NULL,
  PRIMARY KEY (user_id, card_id)
);

CREATE TABLE IF NOT EXISTS user_setting (
  user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
"""


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- schema / migration


def _apply_schema(conn: sqlite3.Connection) -> None:
    """逐句下 DDL，不用 executescript。

    executescript 在有未完成交易時會先隱式 COMMIT —— 遷移就不再是原子的，
    中途失敗會留下半套 schema。用 ; 切開是安全的：這份 DDL 裡沒有字串常值、
    也沒有 trigger／BEGIN…END 區塊。
    """
    for stmt in (s.strip() for s in SCHEMA.split(";")):
        if stmt:
            conn.execute(stmt)


# v1 後期補加的欄位。舊 DB 可能停在更早的版本，搬資料前要先把欄位補齊，
# 否則下面的 INSERT ... SELECT 會指到不存在的欄位。
_V1_ADDED_COLUMNS = {
    "last_reviewed_before": "TEXT",
    "review_count_before": "INTEGER",
    "streak_before": "INTEGER",
    "lapse_before": "INTEGER",
    "feedback_before": "INTEGER",
    "client_event_id": "TEXT",
}

_V1_TABLES = ("card", "review_event", "note", "setting")

_EVENT_COLUMNS = (
    "card_id, deck, reviewed_at, day_key, chosen, correct, feedback, elapsed_ms,"
    " interval_before, interval_after, next_review_before, next_review_after,"
    " last_reviewed_before, review_count_before, streak_before, lapse_before,"
    " feedback_before, review_count_after, streak_after, lapse_after, client_event_id"
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _is_v1(conn: sqlite3.Connection) -> bool:
    """v1 的辨識特徵：有 card 表，而且它沒有 user_id 欄。"""
    if not _table_exists(conn, "card"):
        return False
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(card)")}
    return "user_id" not in cols


def _upgrade_v1_to_v2(conn: sqlite3.Connection) -> None:
    """把單租戶的 v1 資料整批搬給 user 1。

    ALTER TABLE ADD COLUMN 表達不了「把 PK 從 card_id 改成 (user_id, card_id)」，
    所以只能重建表。做法是先把 v1 的表改名讓路，建好新 schema，再 INSERT…SELECT
    搬過去，最後 drop 舊表 —— 全程在同一個交易內，中途失敗就整個回到 v1。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(review_event)")}
    for name, decl in _V1_ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE review_event ADD COLUMN {name} {decl}")

    for t in _V1_TABLES:
        conn.execute(f"ALTER TABLE {t} RENAME TO {t}_v1")

    _apply_schema(conn)

    username = os.environ.get("DL_LEGACY_USERNAME", "max")
    conn.execute(
        "INSERT INTO user(id, username, created_at) VALUES(1, ?, ?)",
        (username, iso(datetime.now(timezone.utc))),
    )
    # v1 啟動時把全部 1896 張卡 seed 進去，那些沒答過的預設值列沒有資訊量，
    # 惰性建列之後更是純粹的噪音 —— 只搬真的有進度的。
    conn.execute(
        """INSERT INTO card_state(user_id, card_id, deck, review_interval_hours,
                                  next_review_at, last_reviewed_at, review_count,
                                  lapse_count, review_streak, last_review_feedback)
           SELECT 1, id, deck, review_interval_hours, next_review_at, last_reviewed_at,
                  review_count, lapse_count, review_streak, last_review_feedback
           FROM card_v1
           WHERE review_count > 0 OR last_reviewed_at IS NOT NULL"""
    )
    # 保留原 id：regrade 靠 ORDER BY id DESC 找「上一次作答」，相對順序不能亂。
    conn.execute(
        f"""INSERT INTO review_event(id, user_id, {_EVENT_COLUMNS})
            SELECT id, 1, {_EVENT_COLUMNS} FROM review_event_v1 ORDER BY id"""
    )
    conn.execute(
        "INSERT INTO note(user_id, card_id, body, updated)"
        " SELECT 1, card_id, body, updated FROM note_v1"
    )
    conn.execute(
        "INSERT INTO user_setting(user_id, key, value) SELECT 1, key, value FROM setting_v1"
    )

    for t in _V1_TABLES:
        conn.execute(f"DROP TABLE {t}_v1")


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        _apply_schema(conn)  # 新增表／索引時補齊，已存在的不動
        return
    if _is_v1(conn):
        _upgrade_v1_to_v2(conn)
    else:
        _apply_schema(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# ---------------------------------------------------------------- connections


def db_path() -> Path:
    return DATA_DIR / "app.db"


def _rollback(conn: sqlite3.Connection) -> None:
    """回捲失敗不能把真正的錯誤擠掉。

    SQLite 遇到 SQLITE_FULL / SQLITE_IOERR 之類會自己把交易收掉，這時再下
    ROLLBACK 會拋「cannot rollback - no transaction is active」，於是「磁碟滿了」
    變成一句莫名其妙的 rollback 錯誤，真正的死因只剩在 __context__ 裡。
    連線關掉時本來就會回捲，所以吞掉這個次要錯誤是安全的。
    """
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # 明示而非沿用 driver 預設（python sqlite3 的 timeout=5.0 剛好也是 5 秒）：
    # 寫入者互相等待的上限是這個 app 的正確性參數，不該由第三方預設值決定。
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    # 刻意不設 synchronous=NORMAL。WAL + NORMAL 會在斷電／OS crash 時丟掉最近幾筆
    # 「已經回 200」的作答，而前端收到 ack 就把它從 outbox 刪了 —— 那幾題永遠補不回來。
    # 換來的是每次 commit 少一次 fsync，在這個一秒鐘不會有兩次寫入的 app 上不值得。


def init(path: Path | None = None) -> None:
    """建檔、建 schema、跑遷移。程序啟動時呼叫一次。

    遷移整包在一個交易裡：半套 schema 比舊 schema 危險得多。
    """
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)
    try:
        _configure(conn)
        conn.execute("PRAGMA journal_mode=WAL")  # 寫進檔案本身，設一次就永久有效
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
            _ensure_default_user(conn)
        except BaseException:
            _rollback(conn)
            raise
        conn.execute("COMMIT")
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
            _rollback(conn)
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


# ---------------------------------------------------------------- users


def _ensure_default_user(conn: sqlite3.Connection) -> None:
    """全新 DB 也要有一個帳號可以掛資料。在 init() 裡建，不在請求路徑上建 ——
    唯讀端點若要順手 INSERT，就得把 BEGIN DEFERRED 升級成寫入交易，WAL 下會
    在剛好有別的寫入者 commit 時拿到 SQLITE_BUSY_SNAPSHOT。
    """
    if conn.execute("SELECT 1 FROM user LIMIT 1").fetchone():
        return
    conn.execute(
        "INSERT INTO user(username, created_at) VALUES(?, ?)",
        (os.environ.get("DL_LEGACY_USERNAME", "max"), iso(datetime.now(timezone.utc))),
    )


def default_user_id(conn: sqlite3.Connection) -> int:
    """尚未接上登入之前的「目前使用者」。

    Phase 4 接上 session cookie 後，這個函式連同 _ensure_default_user 與
    main._current_user 一起刪掉；在那之前它讓單使用者行為與 v1 完全一致
    （遷移過來的進度就是 user 1）。
    """
    return conn.execute("SELECT id FROM user ORDER BY id LIMIT 1").fetchone()["id"]


# ---------------------------------------------------------------- card state


def get_state(
    conn: sqlite3.Connection, user_id: int, card_id: str, cfg: ReviewSettings
) -> CardState:
    """查無列 = 沒答過 = 全新卡。

    以前是啟動時把 1896 張卡全部 seed 進 DB，所以列必然存在。多使用者之後那筆
    帳變成「每註冊一人寫 1896 列」，不能接受；改成答過才建列。derive_state 本來
    就把 review_count == 0 定義成未學習，「沒有列」與「有列但沒答過」語義相同。
    """
    row = conn.execute(
        "SELECT * FROM card_state WHERE user_id=? AND card_id=?", (user_id, card_id)
    ).fetchone()
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


def put_state(
    conn: sqlite3.Connection, user_id: int, card_id: str, deck: str, s: CardState
) -> None:
    """upsert —— 惰性建列之後，第一次作答時該列還不存在。"""
    conn.execute(
        """INSERT INTO card_state(user_id, card_id, deck, review_interval_hours,
                                  next_review_at, last_reviewed_at, review_count,
                                  lapse_count, review_streak, last_review_feedback)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id, card_id) DO UPDATE SET
             review_interval_hours=excluded.review_interval_hours,
             next_review_at=excluded.next_review_at,
             last_reviewed_at=excluded.last_reviewed_at,
             review_count=excluded.review_count,
             lapse_count=excluded.lapse_count,
             review_streak=excluded.review_streak,
             last_review_feedback=excluded.last_review_feedback""",
        (
            user_id,
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


def all_states(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [
        {
            "id": r["card_id"],
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
            """SELECT card_id, deck, review_interval_hours, next_review_at, last_reviewed_at,
                      review_count, lapse_count, review_streak, last_review_feedback
               FROM card_state WHERE user_id=?""",
            (user_id,),
        )
    ]


# ---------------------------------------------------------------- events


def log_event(conn: sqlite3.Connection, **kw) -> None:
    cols = ",".join(kw)
    conn.execute(
        f"INSERT INTO review_event({cols}) VALUES({','.join('?' * len(kw))})",
        tuple(kw.values()),
    )


def event_exists(conn: sqlite3.Connection, user_id: int, client_event_id: str) -> bool:
    """離線補送的冪等閘門：同一個 client_event_id 只能算一次分。

    手機補送到一半斷線、使用者重新整理再送一次，是必然會發生的事；
    沒有這道閘門就會被當成兩次作答，間隔連乘兩次、review_count 多算。

    以帳號為範圍：兩台裝置各自產的 id 有碰撞的可能（時間戳 + 6 位隨機），
    全域唯一會讓 B 的作答被誤判成 A 的重送而整筆消失。
    """
    return conn.execute(
        "SELECT 1 FROM review_event WHERE user_id=? AND client_event_id=?",
        (user_id, client_event_id),
    ).fetchone() is not None


# ---------------------------------------------------------------- notes


def all_notes(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    return {
        r["card_id"]: r["body"]
        for r in conn.execute(
            "SELECT card_id, body FROM note WHERE user_id=?", (user_id,)
        )
    }


# ---------------------------------------------------------------- settings


def get_setting(conn: sqlite3.Connection, user_id: int, key: str, default: str) -> str:
    row = conn.execute(
        "SELECT value FROM user_setting WHERE user_id=? AND key=?", (user_id, key)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, user_id: int, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO user_setting(user_id, key, value) VALUES(?,?,?)
           ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value""",
        (user_id, key, value),
    )


def preset_name(conn: sqlite3.Connection, user_id: int) -> str:
    return get_setting(conn, user_id, "preset", DEFAULT_PRESET)


def reference_now(conn: sqlite3.Connection, user_id: int, now: datetime) -> datetime:
    """凍結時鐘（偷自 kg 的 reviewReferenceDate）：暫停期間到期計算改用 paused_at。

    出國/忙碌時不會累積出一座待複習山。
    """
    if get_setting(conn, user_id, "paused", "0") != "1":
        return now
    return parse(get_setting(conn, user_id, "paused_at", "")) or now
