"""SQLite 存取層。只存「我對這張卡的記憶狀態」，題目內容不進 DB。

DB 檔放 repo 外（預設 ~/driver-license-data/app.db），git pull / reset 不會碰到進度。

每一列使用者資料都掛 user_id。沒有「全域的那一份進度」這種東西 —— 所有讀寫
一律指名擁有者，跨帳號讀到別人的資料在型別上就做不到（函式簽名逼你給 user_id）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .srs import (
    CUSTOM_PRESET,
    DEFAULT_PRESET,
    REMEMBERED,
    CardState,
    ReviewSettings,
    apply_feedback,
    resolve,
)

DATA_DIR = Path(os.environ.get("DL_DATA_DIR", Path.home() / "driver-license-data"))

SCHEMA_VERSION = 4

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
  client_event_id TEXT,
  -- 作答當下生效的 preset 名稱。rebuild_card 要能重現「當時」算出來的結果，
  -- 拿現在的參數去重算整段歷史，等於換一次 preset 就把過去全部改寫。
  -- NULL 只會出現在 v3 之前寫下的事件（那時沒有記錄，資訊已經不在了）。
  preset TEXT,
  -- preset='custom' 專用：那一刻的五個數值（JSON）。具名 preset 存名字就夠，
  -- 因為名字對應的數值永遠不變；custom 的數值會被使用者改，只存名字等於讓
  -- 每次調倍率都把過去整段重寫 —— 正是上面那條註解在防的事。
  preset_params TEXT
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


def _upgrade_v2_to_v3(conn: sqlite3.Connection) -> None:
    """review_event 加 preset 欄。加欄位是 ALTER TABLE 表達得了的，不必重建。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(review_event)")}
    if "preset" not in have:
        conn.execute("ALTER TABLE review_event ADD COLUMN preset TEXT")


def _upgrade_v3_to_v4(conn: sqlite3.Connection) -> None:
    """review_event 加 preset_params 欄（custom 那一刻的數值快照）。

    舊事件留 NULL：它們都是具名 preset，名字查得到數值，不需要快照。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(review_event)")}
    if "preset_params" not in have:
        conn.execute("ALTER TABLE review_event ADD COLUMN preset_params TEXT")


def _rebuild_from_v1(conn: sqlite3.Connection) -> None:
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

    # 索引不會跟著表改名 —— idx_card_queue / idx_event_day / idx_event_card /
    # idx_event_client 四個名字在 v1 與 v2 完全相同，改名後它們仍佔著那四個名字、
    # 只是 tbl_name 指向 _v1 表。接下來 _apply_schema 的 CREATE INDEX IF NOT EXISTS
    # 就會因為同名而靜默 no-op，最後 DROP TABLE 又把它們一起帶走 ——
    # 結果是新表一個索引都沒有，包括那道 UNIQUE(user_id, client_event_id) 冪等閘。
    # 而且下一次重啟時 version>=2 的分支會把索引補回來，故障會自我痊癒，
    # 永遠不會有人發現曾經發生過。所以要在建新 schema 之前先把它們清掉。
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        f" AND tbl_name IN ({','.join('?' * len(_V1_TABLES))})",
        tuple(f"{t}_v1" for t in _V1_TABLES),
    ).fetchall():
        conn.execute(f'DROP INDEX IF EXISTS "{r["name"]}"')

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
    """版本不認得就拒絕開，不要靜默蓋章。

    CREATE TABLE IF NOT EXISTS 對既有表完全無作用，所以「加了版本號卻只改 SCHEMA」
    會變成欄位沒加、版本卻被蓋成新的，之後每次啟動都自認最新。每一段升級都要
    明確寫出來，走不到的版本一律 raise。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema v{version} 比這份程式支援的 v{SCHEMA_VERSION} 新，拒絕開啟"
        )
    if version == SCHEMA_VERSION:
        _apply_schema(conn)  # 新增表／索引時補齊，已存在的不動
        return

    if version == 0:
        # _rebuild_from_v1 走的是 _apply_schema，建出來就是最新形狀，不必再逐級升
        if _is_v1(conn):
            _rebuild_from_v1(conn)
        version = SCHEMA_VERSION
    if version == 2:
        _upgrade_v2_to_v3(conn)
        version = 3
    if version == 3:
        _upgrade_v3_to_v4(conn)
        version = 4

    if version != SCHEMA_VERSION:
        raise RuntimeError(f"沒有 v{version} → v{SCHEMA_VERSION} 的遷移路徑")
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
        except BaseException:
            _rollback(conn)
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def purge_expired_sessions(path: Path | None = None) -> int:
    """把所有帳號的過期 session 列清掉。回傳刪了幾列。

    auth.purge_expired_sessions 只清「正在登入的那個帳號」，掛在登入那條路上。
    問題是過期在 user_for_token 是「查到但判定無效」，列不會自己消失 ——
    一個註冊完就不再登入的帳號，那列會躺在那裡直到有人再登入為止，也就是永遠。
    量很小，但這正是「沒人掃就會慢慢爛」的那一類：放著只會在某天變成一張
    沒有人知道為什麼這麼大的表。程序啟動時掃一次是最省事的時機。
    """
    conn = sqlite3.connect(path or db_path(), isolation_level=None)
    try:
        _configure(conn)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM session WHERE expires_at<=?",
            (iso(datetime.now(timezone.utc)),),
        )
        conn.execute("COMMIT")
        return cur.rowcount
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


def log_event(conn: sqlite3.Connection, user_id: int, **kw) -> None:
    """user_id 是具名參數而不是塞在 **kw 裡：這一層的規矩是「簽名逼你指名擁有者」，
    唯一的例外就是唯一會被漏掉的地方。"""
    kw = {"user_id": user_id, **kw}
    cols = ",".join(kw)
    conn.execute(
        f"INSERT INTO review_event({cols}) VALUES({','.join('?' * len(kw))})",
        tuple(kw.values()),
    )


def event_settings(row) -> ReviewSettings | None:
    """一筆事件當時生效的參數。None＝這筆沒記（v3 之前），呼叫端只能用現在的。

    custom 讀 preset_params 那份快照；具名 preset 一律查表，就算 preset_params
    有東西也不理它 —— 名字與數值對不起來的事件不該有第二種解釋。
    """
    name = row["preset"]
    if not name:
        return None
    if name != CUSTOM_PRESET:
        return ReviewSettings.preset(name)
    raw = row["preset_params"] if "preset_params" in row.keys() else None
    try:
        return resolve(name, json.loads(raw) if raw else None)
    except (ValueError, TypeError):
        # 快照壞掉（手改過 DB、或未來版本寫進了讀不懂的東西）不該讓整段重算炸掉。
        # 退回預設比丟例外好：使用者看到的是排程稍微偏，而不是整個帳號打不開。
        return resolve(name, None)


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


def _events_for_card(conn: sqlite3.Connection, user_id: int, card_id: str) -> list:
    """該卡的全部事件，依作答時刻排序。

    刻意不在 SQL 裡 ORDER BY reviewed_at。那是 ISO 字串，只有在「時區偏移全部相同」
    時文字序才等於時間序 —— 而 reviewed_at 是前端給的，pydantic 會保留它原本的偏移，
    db.iso() 原樣寫進去。實測送 '2026-07-20T10:00:00+08:00' 就以 '+08:00' 存下，
    文字序把它排在 '...T03:00:00+00:00' 之後，但它其實早一小時。

    同刻的兩筆以 id 決勝（等同插入順序）。這種情況不保證跨裝置收斂，但兩台裝置
    在同一毫秒答同一張卡才會發生，代價與複雜度不成比例。
    """
    rows = conn.execute(
        "SELECT * FROM review_event WHERE user_id=? AND card_id=?", (user_id, card_id)
    ).fetchall()
    return sorted(rows, key=lambda r: (parse(r["reviewed_at"]), r["id"]))


def latest_event(conn: sqlite3.Connection, user_id: int, card_id: str):
    """這張卡「時間序上」最後一次作答。

    不是 ORDER BY id DESC ——「最後寫進 DB 的」與「最後作答的」在亂序補送之後
    不是同一筆。regrade 的語義是使用者的「我剛剛那次是猜的」，那指的是時間。
    """
    rows = _events_for_card(conn, user_id, card_id)
    return rows[-1] if rows else None


def has_later_event(
    conn: sqlite3.Connection, user_id: int, card_id: str, at: datetime
) -> bool:
    """這張卡還有比 at 更晚的事件嗎？有的話剛寫進去的那筆就是亂序的。"""
    return any(
        parse(r["reviewed_at"]) > at
        for r in conn.execute(
            "SELECT reviewed_at FROM review_event WHERE user_id=? AND card_id=?",
            (user_id, card_id),
        )
    )


def rebuild_card(
    conn: sqlite3.Connection, user_id: int, card_id: str, deck: str, cfg: ReviewSettings
) -> CardState:
    """把這張卡從事件日誌整段重算，並改寫每筆事件的 before/after 快照。

    為什麼需要：間隔是乘法式的，套用順序不可交換。兩台裝置各自離線刷題再補送時，
    較舊的事件會在較新的狀態之後才抵達，增量套用就是把舊事件疊在新狀態上 ——
    算出來的間隔是錯的，而且沒有任何跡象，只會安靜地把排程歪掉。
    唯一能讓「任何到達順序都收斂到同一個結果」的做法，是回到事件日誌重算。

    快照欄位一起改寫是必要的，不是順手：regrade 靠 before 精確回捲上一次作答，
    重算後不同步的話，那些數字會指向一個從來不存在的狀態。

    每筆事件用「它當時生效的 preset」重算，不是用現在的。拿現在的參數重算整段歷史
    的話，換一次 preset 就會讓之後任何一筆遲到的舊事件把整張卡改寫 —— 實測
    relaxed 下答對三次是 375h，切到 intensive 後來一筆亂序事件會變成 30.7h，
    而且最終狀態重新取決於抵達順序，正好是這個函式要消滅的東西。
    v3 之前的事件沒有記錄 preset，只能用呼叫端給的 cfg（那個資訊已經不在了）。
    """
    state = None
    for r in _events_for_card(conn, user_id, card_id):
        ev_cfg = event_settings(r) or cfg
        # 首答用「該筆事件」的 initial_hours 當 base，所以 fresh() 也要跟著它
        before, at = state or CardState.fresh(ev_cfg), parse(r["reviewed_at"])
        state = apply_feedback(before, r["feedback"] == REMEMBERED, ev_cfg, at)
        conn.execute(
            """UPDATE review_event SET
                 interval_before=?, interval_after=?,
                 next_review_before=?, next_review_after=?,
                 last_reviewed_before=?,
                 review_count_before=?, streak_before=?, lapse_before=?, feedback_before=?,
                 review_count_after=?, streak_after=?, lapse_after=?
               WHERE user_id=? AND id=?""",
            (
                before.review_interval_hours, state.review_interval_hours,
                iso(before.next_review_at), iso(state.next_review_at),
                iso(before.last_reviewed_at),
                before.review_count, before.review_streak, before.lapse_count,
                before.last_review_feedback,
                state.review_count, state.review_streak, state.lapse_count,
                user_id, r["id"],
            ),
        )
    if state is None:  # 事件全被刪光（regrade 刪掉最後一筆的極端情況）
        state = CardState.fresh(cfg)
    put_state(conn, user_id, card_id, deck, state)
    return state


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


def custom_params(conn: sqlite3.Connection, user_id: int) -> dict | None:
    raw = get_setting(conn, user_id, "custom_params", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def user_settings(conn: sqlite3.Connection, user_id: int) -> ReviewSettings:
    """這個帳號現在生效的參數（custom 的話帶上他自己那組數值）。"""
    return resolve(preset_name(conn, user_id), custom_params(conn, user_id))


def reference_now(conn: sqlite3.Connection, user_id: int, now: datetime) -> datetime:
    """凍結時鐘（偷自 kg 的 reviewReferenceDate）：暫停期間到期計算改用 paused_at。

    出國/忙碌時不會累積出一座待複習山。
    """
    if get_setting(conn, user_id, "paused", "0") != "1":
        return now
    return parse(get_setting(conn, user_id, "paused_at", "")) or now
