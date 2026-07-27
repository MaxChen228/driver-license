"""v1（單租戶）→ v2（多租戶）的一次性重建遷移。

這是全專案唯一不可逆的操作：跑完舊表就被 drop 了。所以這裡不只驗「有搬過去」，
還要驗「一個位元都沒歪」—— 遷移錯了不會有人發現，只會安靜地把某人的排程算歪。
"""

import importlib
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.conftest import data_dir_env

# v1 的 schema 原文（commit 493ba62 之前的 app/db.py SCHEMA，加上當時的 ALTER）。
# 刻意抄一份而不是 import 舊程式：遷移要能對付「真實存在過的檔案」，
# 而那份 schema 已經不在程式碼裡了。
V1_SCHEMA = """
CREATE TABLE card (
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
CREATE INDEX idx_card_queue ON card(deck, review_count, next_review_at);
CREATE TABLE review_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL, deck TEXT NOT NULL,
  reviewed_at TEXT NOT NULL, day_key TEXT NOT NULL,
  chosen INTEGER, correct INTEGER NOT NULL, feedback INTEGER NOT NULL, elapsed_ms INTEGER,
  interval_before REAL, interval_after REAL,
  next_review_before TEXT, next_review_after TEXT,
  last_reviewed_before TEXT,
  review_count_before INTEGER, streak_before INTEGER, lapse_before INTEGER, feedback_before INTEGER,
  review_count_after INTEGER, streak_after INTEGER, lapse_after INTEGER,
  client_event_id TEXT
);
CREATE INDEX idx_event_day ON review_event(deck, day_key);
CREATE INDEX idx_event_card ON review_event(card_id);
CREATE UNIQUE INDEX idx_event_client ON review_event(client_event_id);
CREATE TABLE note (card_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# 兩張有進度的卡 + 一張只有 seed 預設值的空卡（v1 啟動時 1896 張全建，絕大多數長這樣）
V1_CARDS = [
    ("C0001", "car", 60.0, "2026-08-01T02:00:00+00:00", "2026-07-20T02:00:00+00:00", 2, 0, 2, 1),
    ("M0001", "moto", 12.0, "2026-07-21T03:00:00+00:00", "2026-07-20T03:00:00+00:00", 1, 1, 0, 0),
    ("C0002", "car", 24.0, None, None, 0, 0, 0, -1),
]


def _build_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.executemany(
        "INSERT INTO card(id, deck, review_interval_hours, next_review_at,"
        " last_reviewed_at, review_count, lapse_count, review_streak,"
        " last_review_feedback) VALUES(?,?,?,?,?,?,?,?,?)",
        V1_CARDS,
    )
    conn.executemany(
        "INSERT INTO review_event(id, card_id, deck, reviewed_at, day_key, chosen,"
        " correct, feedback, elapsed_ms, interval_before, interval_after,"
        " next_review_before, next_review_after, last_reviewed_before,"
        " review_count_before, streak_before, lapse_before, feedback_before,"
        " review_count_after, streak_after, lapse_after, client_event_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "C0001", "car", "2026-07-19T02:00:00+00:00", "2026-07-19", 1, 1, 1,
             1200, 24.0, 60.0, None, "2026-07-21T14:00:00+00:00", None, 0, 0, 0, -1,
             1, 1, 0, "ev-1"),
            (2, "C0001", "car", "2026-07-20T02:00:00+00:00", "2026-07-20", 1, 1, 1,
             900, 60.0, 150.0, "2026-07-21T14:00:00+00:00", "2026-08-01T02:00:00+00:00",
             "2026-07-19T02:00:00+00:00", 1, 1, 0, 1, 2, 2, 0, "ev-2"),
            (3, "M0001", "moto", "2026-07-20T03:00:00+00:00", "2026-07-20", 2, 0, 0,
             3000, 24.0, 12.0, None, "2026-07-21T03:00:00+00:00", None, 0, 0, 0, -1,
             1, 0, 1, None),
        ],
    )
    conn.execute(
        "INSERT INTO note(card_id, body, updated) VALUES(?,?,?)",
        ("C0001", "禁止四輪以上汽車及機車", "2026-07-20T02:05:00+00:00"),
    )
    conn.executemany(
        "INSERT INTO setting(key, value) VALUES(?,?)",
        [("preset", "intensive"), ("paused", "0")],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def migrated(tmp_path):
    """建一個 v1 DB，讓 app 啟動時把它遷移掉，回傳 (TestClient, 連線工廠)。"""
    _build_v1(tmp_path / "app.db")
    os.environ["DL_LEGACY_USERNAME"] = "legacy-owner"
    try:
        with data_dir_env(tmp_path):
            import app.db
            import app.main

            importlib.reload(app.db)
            main = importlib.reload(app.main)
            with TestClient(main.app) as c:
                yield c, lambda: sqlite3.connect(tmp_path / "app.db")
    finally:
        del os.environ["DL_LEGACY_USERNAME"]


def test_legacy_progress_lands_on_one_account(migrated):
    _, connect = migrated
    conn = connect()
    users = conn.execute("SELECT id, username, password_hash FROM user").fetchall()
    assert users == [(1, "legacy-owner", None)]  # 還不能登入，等 useradmin 設密碼


def test_seeded_empty_cards_are_dropped_but_real_progress_is_byte_identical(migrated):
    _, connect = migrated
    conn = connect()
    rows = conn.execute(
        """SELECT user_id, card_id, deck, review_interval_hours, next_review_at,
                  last_reviewed_at, review_count, lapse_count, review_streak,
                  last_review_feedback FROM card_state ORDER BY card_id"""
    ).fetchall()
    # C0002 只有 seed 預設值，沒有資訊量，不搬
    assert [r[1] for r in rows] == ["C0001", "M0001"]
    assert rows[0] == (1, *V1_CARDS[0])
    assert rows[1] == (1, *V1_CARDS[1])


def test_events_keep_their_ids_and_order(migrated):
    _, connect = migrated
    conn = connect()
    rows = conn.execute(
        "SELECT id, user_id, card_id, client_event_id, interval_before, interval_after"
        " FROM review_event ORDER BY id"
    ).fetchall()
    # 原 id 保留：regrade 靠 ORDER BY id DESC 找「上一次作答」
    assert [r[0] for r in rows] == [1, 2, 3]
    assert all(r[1] == 1 for r in rows)
    assert [r[3] for r in rows] == ["ev-1", "ev-2", None]
    assert rows[1][4:] == (60.0, 150.0)


def test_notes_and_settings_move_to_the_account(migrated):
    client, connect = migrated
    conn = connect()
    assert conn.execute("SELECT user_id, card_id, body FROM note").fetchall() == [
        (1, "C0001", "禁止四輪以上汽車及機車")
    ]
    assert dict(
        conn.execute("SELECT key, value FROM user_setting WHERE user_id=1").fetchall()
    ) == {"preset": "intensive", "paused": "0"}
    # 端到端：遷移後 preset 仍然是 intensive，不是掉回預設的 relaxed
    assert client.get("/api/settings").json()["preset"] == "intensive"


def test_v1_tables_are_gone_and_version_is_stamped(migrated):
    _, connect = migrated
    conn = connect()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (names & {"card", "card_v1", "review_event_v1", "note_v1", "setting_v1"})
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_migration_is_idempotent(migrated):
    """再開一次 app 不能重跑遷移（會撞 UNIQUE username 或再搬一次事件）。"""
    import app.db

    _, connect = migrated
    before = connect().execute("SELECT COUNT(*) FROM review_event").fetchone()[0]

    # 直接釘住守衛本身：v2 的 DB 再開，_upgrade_v1_to_v2 一次都不能被呼叫。
    # 只比對事件數是釘不住的 —— 守衛拿掉後 _is_v1 找不到 card 表也會回 False，
    # 走 _apply_schema 全 no-op，數字照樣不變，測試照樣綠。
    called = []
    original = app.db._upgrade_v1_to_v2
    app.db._upgrade_v1_to_v2 = lambda conn: called.append(1)
    try:
        app.db.init()
        app.db.init()
    finally:
        app.db._upgrade_v1_to_v2 = original

    assert called == []
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM review_event").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 1


# 遷移把 v1 的表改名讓路，但索引不會跟著改名，而這四個名字 v1 與 v2 完全相同。
# 沒有明確 DROP 的話：改名後舊索引仍佔著名字 → CREATE INDEX IF NOT EXISTS 靜默
# no-op → DROP TABLE 把舊索引一起帶走 → 新表一個索引都沒有。而且下次重啟會把它們
# 補回來，故障自己痊癒、永遠沒人發現。這是本次遷移最實際的失敗模式。
EXPECTED_INDEXES = {
    "idx_card_queue": "card_state",
    "idx_event_day": "review_event",
    "idx_event_card": "review_event",
    "idx_event_client": "review_event",
    "idx_session_user": "session",
}


def test_indexes_survive_the_rebuild(migrated):
    _, connect = migrated
    got = dict(
        connect().execute(
            "SELECT name, tbl_name FROM sqlite_master"
            " WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()
    )
    assert got == EXPECTED_INDEXES


def test_idempotency_gate_is_enforced_by_the_index_right_after_migrating(migrated):
    """UNIQUE(user_id, client_event_id) 必須「當下」就生效，不是等下次重啟。

    app 層的 event_exists 還在，所以就算索引沒了也看不出來 —— 直到兩個請求
    同時補送同一筆，或有人繞過那道 SELECT。db 層的保證不能只是「遲早會有」。
    """
    import sqlite3

    _, connect = migrated
    conn = connect()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_event(user_id, card_id, deck, reviewed_at, day_key,"
            " correct, feedback, client_event_id)"
            " VALUES(1,'C0001','car','2026-07-21T00:00:00+00:00','2026-07-21',1,1,'ev-1')"
        )


def test_unknown_schema_version_refuses_to_open(tmp_path):
    """認不得的版本要大聲拒絕，不能靜默蓋章成最新版。

    最危險的形狀是日後加了 SCHEMA_VERSION=3 卻只用 CREATE TABLE IF NOT EXISTS ——
    對既有表毫無作用，欄位沒加、版本卻被蓋成 3，之後每次啟動都自認最新。
    """
    import importlib
    import sqlite3

    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()

    with data_dir_env(tmp_path):
        import app.db

        m = importlib.reload(app.db)
        with pytest.raises(RuntimeError, match="比這份程式支援的"):
            m.init()


def test_regrade_still_works_on_migrated_events(migrated):
    """遷移後的事件必須還能被「我是猜的」回捲 —— before 快照要完整搬過來。"""
    client, _ = migrated
    r = client.post("/api/answer/regrade", json={"card_id": "C0001"})
    assert r.status_code == 200
    # 用 ev-2 的 before 快照回捲：60 × 0.35 = 21，review_count 仍是 2（不是 3）。
    # 0.35 是 intensive 的 forgot_mult —— 這個數字同時證明遷移過來的 preset 真的生效了。
    assert r.json()["interval_hours"] == 21.0
    assert r.json()["review_count"] == 2
