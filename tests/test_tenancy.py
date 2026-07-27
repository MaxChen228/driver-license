"""多租戶隔離 —— 直接對 db 層驗，不經過 API。

Phase 4 接上登入之前，HTTP 層永遠只看得到一個帳號，所以「A 看不到 B」這件事
在端點上驗不出來。但隔離是不是真的成立，取決於每一個查詢有沒有帶 user_id ——
那是 db 層的責任，也就在這裡釘死。Phase 4 會再補一層打 API 的版本。
"""

import importlib
import os

import pytest

from app import srs

CFG = srs.PRESETS["relaxed"]
NOW = srs.utcnow()


@pytest.fixture()
def dbmod(tmp_path):
    os.environ["DL_DATA_DIR"] = str(tmp_path)
    import app.db

    m = importlib.reload(app.db)
    m.init()
    return m


@pytest.fixture()
def two_users(dbmod):
    with dbmod.session(write=True) as conn:
        a = conn.execute(
            "INSERT INTO user(username, created_at) VALUES('alice', ?)",
            (dbmod.iso(NOW),),
        ).lastrowid
        b = conn.execute(
            "INSERT INTO user(username, created_at) VALUES('bob', ?)",
            (dbmod.iso(NOW),),
        ).lastrowid
    return dbmod, a, b


def test_same_card_holds_independent_state_per_user(two_users):
    dbmod, a, b = two_users
    answered = srs.apply_feedback(srs.CardState.fresh(CFG), True, CFG, NOW)
    with dbmod.session(write=True) as conn:
        dbmod.put_state(conn, a, "C0001", "car", answered)

    with dbmod.session() as conn:
        assert dbmod.get_state(conn, a, "C0001", CFG).review_count == 1
        assert dbmod.get_state(conn, b, "C0001", CFG).review_count == 0  # B 沒答過
        assert [s["id"] for s in dbmod.all_states(conn, a)] == ["C0001"]
        assert dbmod.all_states(conn, b) == []


def test_client_event_id_is_unique_per_account_not_globally(two_users):
    """兩台裝置各自產的 id 有碰撞可能（時間戳 + 6 位隨機）。全域唯一的話，
    B 的作答會被誤判成 A 的重送而整筆消失 —— 使用者只會看到「有一題沒算到」。
    """
    dbmod, a, b = two_users
    with dbmod.session(write=True) as conn:
        for uid in (a, b):
            dbmod.log_event(
                conn, user_id=uid, card_id="C0001", deck="car",
                reviewed_at=dbmod.iso(NOW), day_key=NOW.date().isoformat(),
                chosen=1, correct=1, feedback=srs.REMEMBERED,
                client_event_id="collision",
            )

    with dbmod.session() as conn:
        assert dbmod.event_exists(conn, a, "collision")
        assert dbmod.event_exists(conn, b, "collision")


def test_duplicate_client_event_id_within_one_account_is_rejected(two_users):
    import sqlite3

    dbmod, a, _ = two_users
    with dbmod.session(write=True) as conn:
        dbmod.log_event(
            conn, user_id=a, card_id="C0001", deck="car", reviewed_at=dbmod.iso(NOW),
            day_key=NOW.date().isoformat(), chosen=1, correct=1,
            feedback=srs.REMEMBERED, client_event_id="dup",
        )
    with pytest.raises(sqlite3.IntegrityError):
        with dbmod.session(write=True) as conn:
            dbmod.log_event(
                conn, user_id=a, card_id="C0001", deck="car", reviewed_at=dbmod.iso(NOW),
                day_key=NOW.date().isoformat(), chosen=1, correct=1,
                feedback=srs.REMEMBERED, client_event_id="dup",
            )


def test_notes_and_settings_do_not_leak(two_users):
    dbmod, a, b = two_users
    with dbmod.session(write=True) as conn:
        conn.execute(
            "INSERT INTO note(user_id, card_id, body, updated) VALUES(?,?,?,?)",
            (a, "C0001", "alice 的筆記", dbmod.iso(NOW)),
        )
        dbmod.set_setting(conn, a, "preset", "intensive")

    with dbmod.session() as conn:
        assert dbmod.all_notes(conn, a) == {"C0001": "alice 的筆記"}
        assert dbmod.all_notes(conn, b) == {}
        assert dbmod.preset_name(conn, a) == "intensive"
        assert dbmod.preset_name(conn, b) == "relaxed"  # 預設值，不是 A 的設定


def test_deleting_a_user_cascades_everything(two_users):
    """刪帳號必須是一行 DELETE。手動一張表一張表清，總有一天會漏掉新加的那張。"""
    dbmod, a, b = two_users
    state = srs.apply_feedback(srs.CardState.fresh(CFG), True, CFG, NOW)
    with dbmod.session(write=True) as conn:
        for uid in (a, b):
            dbmod.put_state(conn, uid, "C0001", "car", state)
            dbmod.set_setting(conn, uid, "preset", "intensive")
            conn.execute(
                "INSERT INTO note(user_id, card_id, body, updated) VALUES(?,?,?,?)",
                (uid, "C0001", "n", dbmod.iso(NOW)),
            )
            dbmod.log_event(
                conn, user_id=uid, card_id="C0001", deck="car",
                reviewed_at=dbmod.iso(NOW), day_key=NOW.date().isoformat(),
                chosen=1, correct=1, feedback=srs.REMEMBERED,
            )

    with dbmod.session(write=True) as conn:
        conn.execute("DELETE FROM user WHERE id=?", (a,))

    with dbmod.session() as conn:
        for table in ("card_state", "review_event", "note", "user_setting"):
            left = conn.execute(
                f"SELECT user_id FROM {table}"  # noqa: S608 — table 名來自上面的字面量
            ).fetchall()
            assert [r["user_id"] for r in left] == [b], table
