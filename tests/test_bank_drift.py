"""題庫改版後，DB 裡留下的孤兒列不能污染計數。

題庫改版的標準流程是「重跑 tools/ pipeline 覆蓋 JSON」，刻意不做 migration
（題目留 JSON、DB 只存進度）。所以監理所刪掉一題之後，那個題號的進度列會留在
card_state 裡永遠孤懸。這種列不會有人去清，也不該有人去清 —— 但每一個「用列數
反推題數」的算式都會被它算歪，而且是安靜地歪。
"""

import sqlite3

from app import db, srs

CFG = srs.PRESETS["relaxed"]


def _insert_ghosts(data_dir, n):
    """插進 n 筆題庫裡不存在的題號，模擬題庫改版後的殘留。"""
    conn = sqlite3.connect(data_dir / "app.db")
    # 與 _current_user 同源，不假設遷移一定給到 id 1
    uid = conn.execute("SELECT id FROM user ORDER BY id LIMIT 1").fetchone()[0]
    now = srs.utcnow()
    state = srs.apply_feedback(srs.CardState.fresh(CFG), True, CFG, now)
    for i in range(n):
        conn.execute(
            """INSERT INTO card_state(user_id, card_id, deck, review_interval_hours,
                                      next_review_at, last_reviewed_at, review_count,
                                      lapse_count, review_streak, last_review_feedback)
               VALUES(?, ?, 'car', ?, ?, ?, 1, 0, 1, 1)""",
            (uid, f"GHOST{i}", state.review_interval_hours,
             db.iso(state.next_review_at), db.iso(state.last_reviewed_at)),
        )
    conn.commit()
    conn.close()


def test_orphan_rows_do_not_distort_deck_counts(client, tmp_path):
    _insert_ghosts(tmp_path, 3)
    car = next(d for d in client.get("/api/decks").json()["decks"] if d["deck"] == "car")
    assert car["total"] == 1090
    # 三筆孤兒列被忽略，不是被算成「已複習」再從總數扣掉
    assert car["unlearned"] == 1090
    assert car["due"] == 0 and car["reviewed"] == 0


def test_orphan_rows_cannot_push_unlearned_negative(client, tmp_path):
    """拿列數去減題庫總數的版本，殘留列夠多時會算出負數且不會報錯。"""
    _insert_ghosts(tmp_path, 1200)
    car = next(d for d in client.get("/api/decks").json()["decks"] if d["deck"] == "car")
    assert car["unlearned"] == 1090
    assert car["unlearned"] + car["due"] + car["reviewed"] == car["total"]


def test_orphan_rows_stay_out_of_queues_and_browse(client, tmp_path):
    _insert_ghosts(tmp_path, 3)
    q = client.get("/api/queue/all", params={"deck": "car"}).json()
    assert not any(i.startswith("GHOST") for i in q["card_ids"])
    items = client.get("/api/browse", params={"deck": "car"}).json()
    assert items["count"] == 1090
