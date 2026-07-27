"""driver-license —— 駕照筆試刷題 app。

felix 內網服務，綁 Tailscale 介面（100.118.39.104:8006），tailnet 即邊界、無認證。
絕不掛 Cloudflare Tunnel。
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, srs
from .deck import DATA_DIR, DECKS, all_cards, load_decks

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="driver-license", docs_url="/api/docs", redoc_url=None)

DECK_DATA = load_decks()
CONN = db.connect()
db.seed_cards(CONN, all_cards(DECK_DATA))


def _cfg() -> srs.ReviewSettings:
    return srs.ReviewSettings.preset(db.preset_name(CONN))


def _now(at: datetime | None = None):
    """作答時刻。

    離線補送會帶著「當初真正作答的時間」回來，否則整批會被記成同一刻，
    間隔與熱力圖都會失真。凍結時鐘仍然優先（暫停期間一律用 paused_at）。
    """
    return db.reference_now(CONN, at or srs.utcnow())


def _deck_or_404(deck: str):
    if deck not in DECK_DATA:
        raise HTTPException(404, f"unknown deck: {deck}")
    return DECK_DATA[deck]


def _rows(deck: str):
    return CONN.execute(
        "SELECT id, review_count, next_review_at FROM card WHERE deck=?", (deck,)
    ).fetchall()


def _states(deck: str) -> dict[str, str]:
    now = _now()
    return {
        r["id"]: srs.derive_state(r["review_count"], db.parse(r["next_review_at"]), now)
        for r in _rows(deck)
    }


def _note(card_id: str) -> str | None:
    row = CONN.execute("SELECT body FROM note WHERE card_id=?", (card_id,)).fetchone()
    return row["body"] if row else None


# ---------------------------------------------------------------- decks / queues


@app.get("/api/decks")
def decks():
    out = []
    for key in DECKS:
        d = DECK_DATA[key]
        c = Counter(_states(key).values())
        out.append(
            {
                "deck": key,
                "label": d.label,
                "title": d.title,
                "version": d.version,
                "total": len(d.questions),
                "unlearned": c.get(srs.UNLEARNED, 0),
                "due": c.get(srs.DUE, 0),
                "reviewed": c.get(srs.REVIEWED, 0),
                "categories": d.categories,
            }
        )
    return {
        "decks": out,
        "preset": db.preset_name(CONN),
        "paused": db.get_setting(CONN, "paused", "0") == "1",
    }


@app.get("/api/queue/{kind}")
def queue(
    kind: str,
    deck: str = Query(...),
    category: str | None = None,
    shuffle: bool = False,
    limit: int = Query(0, ge=0),
):
    """三個入口（照 kg 的 Menu 結構）：unlearned / due / all。刻意沒有每日上限。"""
    if kind not in ("unlearned", "due", "all"):
        raise HTTPException(404, f"unknown queue: {kind}")
    d = _deck_or_404(deck)
    now = _now()

    meta = {r["id"]: r for r in _rows(deck)}
    unlearned, due = [], []
    for q in d.questions:
        if category and q["category"] != category:
            continue
        r = meta.get(q["id"])
        if r is None:
            continue
        nxt = db.parse(r["next_review_at"])
        st = srs.derive_state(r["review_count"], nxt, now)
        if st == srs.UNLEARNED:
            unlearned.append(q["id"])
        elif st == srs.DUE:
            due.append((nxt, q["id"]))

    due.sort(key=lambda t: (t[0] or now))  # 到期由舊到新
    due_ids = [i for _, i in due]
    ids = {"unlearned": unlearned, "due": due_ids, "all": due_ids + unlearned}[kind]

    if shuffle:
        random.shuffle(ids)
    if limit:
        ids = ids[:limit]
    return {"deck": deck, "kind": kind, "count": len(ids), "card_ids": ids}


@app.get("/api/card/{card_id}")
def card(card_id: str):
    for d in DECK_DATA.values():
        q = d.by_id.get(card_id)
        if q:
            break
    else:
        raise HTTPException(404, f"unknown card: {card_id}")

    s = db.get_state(CONN, card_id)
    now = _now()
    return {
        "question": q,
        "state": srs.derive_state(s.review_count, s.next_review_at, now) if s else srs.UNLEARNED,
        "review": {
            "interval_hours": s.review_interval_hours,
            "next_review_at": db.iso(s.next_review_at),
            "review_count": s.review_count,
            "lapse_count": s.lapse_count,
            "streak": s.review_streak,
            "last_feedback": s.last_review_feedback,
        }
        if s
        else None,
        "note": _note(card_id),
    }


# ---------------------------------------------------------------- answering


class AnswerIn(BaseModel):
    card_id: str
    chosen: int = Field(ge=1, le=3)
    elapsed_ms: int | None = None
    guessed: bool = False  # 三選一有 33% 猜對率 —— 按了就把答對覆寫成 forgot
    reviewed_at: datetime | None = None  # 離線作答的真實時刻
    client_event_id: str | None = None   # 冪等鍵，重覆補送只算一次


class RegradeIn(BaseModel):
    card_id: str


class SyncIn(BaseModel):
    answers: list[AnswerIn] = []
    notes: dict[str, str] = {}


def _question(card_id: str) -> dict:
    for d in DECK_DATA.values():
        q = d.by_id.get(card_id)
        if q:
            return q
    raise HTTPException(404, f"unknown card: {card_id}")


def _record(q: dict, chosen: int | None, correct: bool, remembered: bool,
            elapsed_ms: int | None, before: srs.CardState,
            reviewed_at: datetime | None = None,
            client_event_id: str | None = None) -> dict:
    """呼叫端必須已持有 db.WRITE_LOCK（regrade 的 DELETE + 重打要在同一把鎖內）。"""
    now = _now(reviewed_at)
    after = srs.apply_feedback(before, remembered, _cfg(), now)
    db.put_state(CONN, q["id"], after)
    db.log_event(
        CONN,
        card_id=q["id"],
        deck=q["deck"],
        reviewed_at=db.iso(now),
        day_key=now.date().isoformat(),
        chosen=chosen,
        correct=int(correct),
        feedback=srs.REMEMBERED if remembered else srs.FORGOT,
        elapsed_ms=elapsed_ms,
        interval_before=before.review_interval_hours,
        interval_after=after.review_interval_hours,
        next_review_before=db.iso(before.next_review_at),
        next_review_after=db.iso(after.next_review_at),
        last_reviewed_before=db.iso(before.last_reviewed_at),
        review_count_before=before.review_count,
        streak_before=before.review_streak,
        lapse_before=before.lapse_count,
        feedback_before=before.last_review_feedback,
        review_count_after=after.review_count,
        streak_after=after.review_streak,
        lapse_after=after.lapse_count,
        client_event_id=client_event_id,
    )
    CONN.commit()  # 每答即寫：不抄 kg 的批次 flush（那是 SwiftData 的問題，不是這裡的）
    return {
        "card_id": q["id"],
        "correct": correct,
        "answer": q["answer"],
        "remembered": remembered,
        "interval_hours": after.review_interval_hours,
        "next_review_at": db.iso(after.next_review_at),
        "review_count": after.review_count,
        "streak": after.review_streak,
        "lapse_count": after.lapse_count,
    }


def _apply_answer(payload: AnswerIn) -> dict:
    """呼叫端必須已持有 db.WRITE_LOCK。"""
    q = _question(payload.card_id)
    before = db.get_state(CONN, payload.card_id)
    if before is None:
        raise HTTPException(404, "card not seeded")
    correct = payload.chosen == q["answer"]
    return _record(
        q, payload.chosen, correct, correct and not payload.guessed,
        payload.elapsed_ms, before, payload.reviewed_at, payload.client_event_id,
    )


@app.post("/api/answer")
def answer(payload: AnswerIn):
    with db.WRITE_LOCK:
        if payload.client_event_id and db.event_exists(CONN, payload.client_event_id):
            raise HTTPException(409, "duplicate client_event_id")
        return _apply_answer(payload)


@app.post("/api/answer/regrade")
def regrade(payload: RegradeIn):
    """把「上一次作答」改判為 forgot（前端的「我是猜的」）。

    不是再送一次 answer —— 那會被當成第二次作答，從已經放大的間隔再乘 0.5，
    數字會錯（60h → 30h，而不是應有的 12h），review_count 也會多一次。
    這裡先用事件裡的 before 快照把卡片精確回捲，刪掉該事件，再以 forgot 重打一次。
    """
    q = _question(payload.card_id)
    with db.WRITE_LOCK:
        ev = CONN.execute(
            "SELECT * FROM review_event WHERE card_id=? ORDER BY id DESC LIMIT 1",
            (payload.card_id,),
        ).fetchone()
        if ev is None:
            raise HTTPException(409, "no answer to regrade")
        if ev["feedback"] == srs.FORGOT:
            raise HTTPException(409, "already graded as forgot")
        if ev["review_count_before"] is None:
            raise HTTPException(409, "event predates regrade support")

        before = srs.CardState(
            review_interval_hours=ev["interval_before"],
            next_review_at=db.parse(ev["next_review_before"]),
            last_reviewed_at=db.parse(ev["last_reviewed_before"]),
            review_count=ev["review_count_before"],
            lapse_count=ev["lapse_before"],
            review_streak=ev["streak_before"],
            last_review_feedback=ev["feedback_before"],
        )
        CONN.execute("DELETE FROM review_event WHERE id=?", (ev["id"],))
        return _record(
            q, ev["chosen"], bool(ev["correct"]), False, ev["elapsed_ms"], before,
            db.parse(ev["reviewed_at"]), ev["client_event_id"],
        )


# ---------------------------------------------------------------- offline sync


def _bootstrap_payload() -> dict:
    """前端離線運作所需的一切（題目本身除外，那是 /data/*.json 由 SW 預先快取）。

    有了它，前端就能在完全沒有網路時自己算出 unlearned/due/reviewed、建佇列、
    跑排程 —— 伺服器只是同步的目的地，不是每翻一張卡的必要條件。
    """
    cfg = _cfg()
    return {
        "server_time": db.iso(srs.utcnow()),
        "reference_time": db.iso(_now()),  # 凍結時鐘生效時會與 server_time 不同
        "preset": db.preset_name(CONN),
        "presets": list(srs.PRESETS),
        "paused": db.get_setting(CONN, "paused", "0") == "1",
        "paused_at": db.get_setting(CONN, "paused_at", "") or None,
        "params": {
            "initial_hours": cfg.initial_hours,
            "remembered_mult": cfg.remembered_mult,
            "forgot_mult": cfg.forgot_mult,
            "min_hours": cfg.min_hours,
            "max_hours": cfg.max_hours,
        },
        "decks": [
            {
                "deck": k,
                "label": DECK_DATA[k].label,
                "title": DECK_DATA[k].title,
                "version": DECK_DATA[k].version,
                "total": len(DECK_DATA[k].questions),
                "categories": DECK_DATA[k].categories,
                "url": f"/data/{k}.json",
            }
            for k in DECKS
        ],
        "states": db.all_states(CONN),
        "notes": db.all_notes(CONN),
    }


@app.get("/api/bootstrap")
def bootstrap():
    return _bootstrap_payload()


@app.post("/api/sync")
def sync(payload: SyncIn):
    """補送離線期間累積的作答與筆記，回傳權威狀態讓前端整份覆蓋。

    照 reviewed_at 由舊到新重放 —— 順序錯了間隔就會算錯（同一張卡答兩次，
    先後不同結果不同）。已經送成功過的 client_event_id 直接跳過，不重複計分。
    """
    applied, skipped, failed = [], [], []
    ordered = sorted(
        payload.answers,
        key=lambda a: (a.reviewed_at or srs.utcnow(), a.client_event_id or ""),
    )
    with db.WRITE_LOCK:
        for a in ordered:
            if a.client_event_id and db.event_exists(CONN, a.client_event_id):
                skipped.append(a.client_event_id)
                continue
            try:
                _apply_answer(a)
                applied.append(a.client_event_id)
            except HTTPException as exc:
                failed.append({"client_event_id": a.client_event_id, "detail": exc.detail})

        for card_id, body in payload.notes.items():
            text = body.strip()
            if text:
                CONN.execute(
                    """INSERT INTO note(card_id, body, updated) VALUES(?,?,?)
                       ON CONFLICT(card_id) DO UPDATE SET body=excluded.body,
                                                          updated=excluded.updated""",
                    (card_id, text, db.iso(srs.utcnow())),
                )
            else:
                CONN.execute("DELETE FROM note WHERE card_id=?", (card_id,))
        CONN.commit()

    out = _bootstrap_payload()
    out["sync"] = {"applied": applied, "skipped": skipped, "failed": failed}
    return out


# ---------------------------------------------------------------- notes


class NoteIn(BaseModel):
    body: str


@app.get("/api/note/{card_id}")
def get_note(card_id: str):
    return {"card_id": card_id, "body": _note(card_id) or ""}


@app.put("/api/note/{card_id}")
def put_note(card_id: str, payload: NoteIn):
    body = payload.body.strip()
    if body:
        CONN.execute(
            """INSERT INTO note(card_id, body, updated) VALUES(?,?,?)
               ON CONFLICT(card_id) DO UPDATE SET body=excluded.body, updated=excluded.updated""",
            (card_id, body, db.iso(srs.utcnow())),
        )
    else:
        CONN.execute("DELETE FROM note WHERE card_id=?", (card_id,))
    CONN.commit()
    return {"card_id": card_id, "body": body}


# ---------------------------------------------------------------- browse / stats


@app.get("/api/browse")
def browse(
    deck: str = Query(...),
    q: str | None = None,
    category: str | None = None,
    wrong_only: bool = False,
    noted_only: bool = False,
):
    d = _deck_or_404(deck)
    states = _states(deck)
    lapsed = {
        r["id"]
        for r in CONN.execute(
            "SELECT id FROM card WHERE deck=? AND lapse_count > 0", (deck,)
        ).fetchall()
    }
    noted = {
        r["card_id"] for r in CONN.execute("SELECT card_id FROM note").fetchall()
    }

    out = []
    for q_ in d.questions:
        if category and q_["category"] != category:
            continue
        if wrong_only and q_["id"] not in lapsed:
            continue
        if noted_only and q_["id"] not in noted:
            continue
        if q:
            hay = q_["stem"] + "".join(o["text"] for o in q_["options"])
            if q not in hay:
                continue
        out.append(
            {
                "id": q_["id"],
                "number": q_["number"],
                "stem": q_["stem"],
                "stem_is_image": q_["stem_is_image"],
                "category": q_["category"],
                "state": states.get(q_["id"], srs.UNLEARNED),
                "lapsed": q_["id"] in lapsed,
                "noted": q_["id"] in noted,
            }
        )
    return {"deck": deck, "count": len(out), "items": out}


@app.get("/api/stats")
def stats(deck: str = Query(...)):
    _deck_or_404(deck)
    now = _now()

    # 14 日到期預測（今天以前全部收攏進今天，照 kg 的 forecast 做法）
    forecast: Counter[str] = Counter()
    for r in _rows(deck):
        if r["review_count"] == 0:
            continue
        nxt = db.parse(r["next_review_at"])
        if nxt is None:
            continue
        day = max((nxt.date() - now.date()).days, 0)
        if day < 14:
            forecast[(now.date() + timedelta(days=day)).isoformat()] += 1

    rows = CONN.execute(
        """SELECT day_key, COUNT(*) n, SUM(correct) ok FROM review_event
           WHERE deck=? GROUP BY day_key ORDER BY day_key""",
        (deck,),
    ).fetchall()
    activity = [{"day": r["day_key"], "count": r["n"], "correct": r["ok"] or 0} for r in rows]

    # 連續天數（往回數到第一個沒作答的日子）
    days = {a["day"] for a in activity}
    streak = 0
    cursor = now.date()
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)

    worst = CONN.execute(
        """SELECT id, lapse_count FROM card
           WHERE deck=? AND lapse_count > 0 ORDER BY lapse_count DESC LIMIT 20""",
        (deck,),
    ).fetchall()
    d = DECK_DATA[deck]
    by_cat: Counter[str] = Counter()
    for r in CONN.execute(
        "SELECT id, lapse_count FROM card WHERE deck=? AND lapse_count > 0", (deck,)
    ).fetchall():
        q_ = d.by_id.get(r["id"])
        if q_:
            by_cat[q_["category"]] += r["lapse_count"]

    total = sum(a["count"] for a in activity)
    ok = sum(a["correct"] for a in activity)
    return {
        "deck": deck,
        "answered_total": total,
        "accuracy": round(ok / total, 4) if total else None,
        "streak_days": streak,
        "forecast_14d": [
            {"day": (now.date() + timedelta(days=i)).isoformat(),
             "count": forecast.get((now.date() + timedelta(days=i)).isoformat(), 0)}
            for i in range(14)
        ],
        "activity": activity[-180:],
        "worst_cards": [{"id": r["id"], "lapses": r["lapse_count"]} for r in worst],
        "worst_categories": [{"category": k, "lapses": v} for k, v in by_cat.most_common(10)],
    }


# ---------------------------------------------------------------- settings


class SettingsIn(BaseModel):
    preset: str | None = None
    paused: bool | None = None


@app.get("/api/settings")
def get_settings():
    cfg = _cfg()
    return {
        "preset": db.preset_name(CONN),
        "presets": list(srs.PRESETS),
        "paused": db.get_setting(CONN, "paused", "0") == "1",
        "paused_at": db.get_setting(CONN, "paused_at", "") or None,
        "params": {
            "initial_hours": cfg.initial_hours,
            "remembered_mult": cfg.remembered_mult,
            "forgot_mult": cfg.forgot_mult,
            "min_hours": cfg.min_hours,
            "max_hours": cfg.max_hours,
        },
    }


@app.put("/api/settings")
def put_settings(payload: SettingsIn):
    if payload.preset is not None:
        if payload.preset not in srs.PRESETS:
            raise HTTPException(400, f"unknown preset: {payload.preset}")
        db.set_setting(CONN, "preset", payload.preset)
    if payload.paused is not None:
        if payload.paused:
            # 進入暫停時把當下時間凍住，之後所有到期計算都用它
            db.set_setting(CONN, "paused_at", db.iso(srs.utcnow()))
            db.set_setting(CONN, "paused", "1")
        else:
            db.set_setting(CONN, "paused", "0")
    return get_settings()


# ---------------------------------------------------------------- static


class _Static(StaticFiles):
    """StaticFiles 加上明確的 cache-control: no-cache。

    預設只送 etag / last-modified、不送 cache-control，瀏覽器於是套用「啟發式
    新鮮度」——沒有明示有效期時，拿 last-modified 至今時間的 10% 當有效期。
    對一個放了幾個月的 style.css 來說那是好幾天。

    後果不是「使用者晚幾天看到新版」而已：service worker 安裝時
    `cache.addAll(SHELL)` 的請求也會先過 HTTP 快取，於是新版 SW 把舊的 CSS
    裝進新的 Cache Storage，變成新 HTML ＋ 新 JS ＋ 舊 CSS 的組合，而且會一直
    留在那個版本裡。實際踩過一次。

    no-cache 不是不快取，是每次都拿 etag 跟伺服器對帳（幾乎都回 304）。
    離線能力本來就由 SW 的 Cache Storage 提供，不靠瀏覽器這層。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["cache-control"] = "no-cache"
        return resp


app.mount("/images", _Static(directory=DATA_DIR / "images"), name="images")
app.mount("/static", _Static(directory=ROOT / "static"), name="static")


@app.get("/data/{deck}.json")
def deck_json(deck: str):
    """整份題庫。service worker 預先快取這個 URL，離線時題目直接從 Cache Storage 讀。

    兩份加起來 1.5 MB、gzip 後 179 KB —— 整包收進手機是零負擔的。
    """
    _deck_or_404(deck)
    return FileResponse(
        DATA_DIR / f"{deck}_questions.json",
        media_type="application/json",
        headers={"cache-control": "no-cache"},  # 由 SW 管快取，瀏覽器別自己壓一層舊的
    )


@app.get("/api/image-manifest")
def image_manifest():
    """354 張題目圖的清單，給 service worker 逐一 precache。"""
    files = sorted(
        f"/images/{p.relative_to(DATA_DIR / 'images').as_posix()}"
        for p in (DATA_DIR / "images").rglob("*.png")
    )
    return {"count": len(files), "files": files}


# sw.js 與 manifest 必須從根目錄供應：service worker 的控制範圍是它自己的路徑，
# 掛在 /static/ 下就只能管 /static/ 底下的請求，整個 app 都不會被攔截。
@app.get("/sw.js")
def service_worker():
    return FileResponse(
        ROOT / "static" / "sw.js",
        media_type="application/javascript",
        headers={"cache-control": "no-cache", "service-worker-allowed": "/"},
    )


@app.get("/manifest.webmanifest")
def web_manifest():
    return FileResponse(
        ROOT / "static" / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"cache-control": "no-cache"},
    )


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html",
                        headers={"cache-control": "no-cache"})
