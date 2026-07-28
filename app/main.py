"""driver-license —— 駕照筆試刷題 app。

跑在 felix，綁 127.0.0.1:8006。對外有兩條路：tailnet 內的 tailscale serve，
以及 Cloudflare Tunnel（drive.wordnexus.lol）。後者推翻了本專案早期
「tailnet 即邊界、無認證、絕不掛 CF Tunnel」的決定 —— 邊界改由這一層自己守。

身分模型有兩種，刻意的：
  匿名  題庫、SRS、狀態全部在瀏覽器（IndexedDB），伺服器一列都不寫。
        公網上的未認證流量因此碰不到 DB，防濫用面歸零。
  帳號  只為了跨裝置同步而存在。登入後伺服器才是權威來源。
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Annotated

from pydantic import BaseModel, Field

from . import auth, db, ratelimit, srs
from .deck import DATA_DIR, DECKS, load_decks

ROOT = Path(__file__).resolve().parent.parent

# 正式站關掉 API 文件：它會把每一個端點、每一個欄位攤開給任何人看。
# openapi_url 一定要一起關 —— 只關 docs_url 的話 /openapi.json 照樣公開，
# 那才是真正有價值的那份（實測過：docs_url=None 時 /openapi.json 仍回 200）。
IS_PROD = os.environ.get("DL_ENV") == "prod"

# 危險感知影片放哪。空字串（預設）＝ 由本服務從 data/videos/ 供應，開發與內網用。
# 設成 "https://videos.wordnexus.lol" 就改由 R2 直出：333 MB 走家用上行撐不住，
# 而 R2 的 egress 免費、又在同一個 CF 帳號底下。
# 一個環境變數同時決定三件事，不能各自為政：前端組網址、CSP 開口、SW 略過快取。
VIDEO_ORIGIN = os.environ.get("DL_VIDEO_ORIGIN", "").rstrip("/")

app = FastAPI(
    title="driver-license",
    docs_url=None if IS_PROD else "/api/docs",
    openapi_url=None if IS_PROD else "/openapi.json",
    redoc_url=None,
)

DECK_DATA = load_decks()
db.init()
# 過期 session 只有在「該帳號自己再登入一次」時才會被清掉，所以註冊完就不再
# 登入的帳號，那列會躺到永遠。啟動時掃一次，一句 DELETE。
_purged = db.purge_expired_sessions()

# 直接寫 stderr，不走 logging：import 期發出的 INFO 沒有任何 handler 接
# （root 的 lastResort 門檻是 WARNING，uvicorn 的 LOGGING_CONFIG 也不碰 root），
# 整行會被安靜丟掉。實測以 DL_ENV=prod 啟動時 log 裡根本沒有這行。
# 這行的用途正是「secrets 檔漏了或 DL_ENV 打錯時要看得出來」，不能自己先消失。
print(
    f"driver-license: env={'prod' if IS_PROD else 'dev'}"
    f" docs={'off' if IS_PROD else '/api/docs'}"
    f" turnstile={'on' if os.environ.get('DL_TURNSTILE_SECRET') else 'off'}"
    f" purged_sessions={_purged}",
    file=sys.stderr, flush=True,
)

# 只有登入的人有伺服器端資料，所以幾乎每個 API 都掛這個相依。
# 型別上就拿不到「沒有 user 的 conn 操作」——db.* 每個函式都要求指名 user_id。
CurrentUser = Depends(auth.require_user)


def _cfg(conn, user_id: int) -> srs.ReviewSettings:
    return srs.ReviewSettings.preset(db.preset_name(conn, user_id))


def _now(conn, user_id: int, at: datetime | None = None):
    """作答時刻。

    帶了 at（離線補送附上的真實作答時刻）就用它，凍結時鐘不得覆蓋。
    原本是反過來的：暫停期間一律改用 paused_at，連補送的歷史事件也一起塌成那一刻。
    後果有三個，而且都不會有跡象：
      1. 整批事件拿到同一個 reviewed_at，亂序偵測的 `>` 永遠不成立，
         重算的順序退化成插入順序 —— 收斂保證在暫停期間整個失效。
         實測 4 個事件的兩種抵達順序會得到 7.5h 與 15h，差兩倍。
      2. day_key 全部記成暫停當天，熱力圖與連續天數失真。
      3. 「上週二答的那題」被記成「按下暫停的那一刻」，事實被改寫。
    凍結時鐘要凍的是「現在幾點」，不是「那件事什麼時候發生的」——
    所以只在沒有 at（真的在當下作答）時才問 reference_now。
    """
    if at is not None:
        return at
    return db.reference_now(conn, user_id, srs.utcnow())


def _deck_or_404(deck: str):
    if deck not in DECK_DATA:
        raise HTTPException(404, f"unknown deck: {deck}")
    return DECK_DATA[deck]


def _rows(conn, user_id: int, deck: str):
    """已作答過的卡（惰性建列：沒答過就沒有列）。"""
    return conn.execute(
        "SELECT card_id, review_count, next_review_at FROM card_state"
        " WHERE user_id=? AND deck=?",
        (user_id, deck),
    ).fetchall()


def _states(conn, user_id: int, deck: str) -> dict[str, str]:
    """只含有列的卡。未列出的一律視為 unlearned。"""
    now = _now(conn, user_id)
    return {
        r["card_id"]: srs.derive_state(r["review_count"], db.parse(r["next_review_at"]), now)
        for r in _rows(conn, user_id, deck)
    }


def _note(conn, user_id: int, card_id: str) -> str | None:
    row = conn.execute(
        "SELECT body FROM note WHERE user_id=? AND card_id=?", (user_id, card_id)
    ).fetchone()
    return row["body"] if row else None


# ---------------------------------------------------------------- 安全標頭 / CSRF


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """兩件事：擋跨站寫入，以及送出安全標頭。

    CSRF 這裡疊三層，沒有一層是多餘的：
      1. cookie 是 SameSite=Lax —— 跨站的 POST 根本不會帶上它。
      2. 不開 CORS —— 瀏覽器擋掉跨來源讀取結果。
      3. 所有寫入要求 X-DL-Client 標頭 —— 自訂標頭會觸發 preflight，
         而我們不回應 preflight，所以任何跨站表單／img 標籤式的偽造直接死在這裡。
    第三層是唯一不依賴瀏覽器正確實作 SameSite 的一層，成本一行，留著。
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and \
            request.url.path.startswith("/api/") and \
            request.headers.get("x-dl-client") != "1":
        return JSONResponse({"detail": "missing X-DL-Client"}, status_code=403)

    response = await call_next(request)
    csp = [
        "default-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
    # 影片搬到 R2 之後就不再是同源，而 media-src 沒寫的話會 fallback 到
    # default-src 'self' —— <video> 會被 CSP 擋掉，畫面上是一塊放不出來的黑框，
    # console 以外沒有任何跡象。img-src 一併開，poster 走的是 img-src 不是 media-src。
    if VIDEO_ORIGIN:
        csp.append(f"media-src 'self' {VIDEO_ORIGIN}")
        csp[1] = f"img-src 'self' data: {VIDEO_ORIGIN}"
    # Turnstile 要載入 challenges.cloudflare.com 的 script 與 iframe。沒啟用就不要開這個口。
    if TURNSTILE_SITEKEY:
        csp.append("script-src 'self' https://challenges.cloudflare.com")
        csp.append("frame-src https://challenges.cloudflare.com")
    else:
        csp.append("script-src 'self'")
    response.headers["content-security-policy"] = "; ".join(csp)
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "same-origin"
    response.headers["permissions-policy"] = "geolocation=(), camera=(), microphone=()"
    return response


# ---------------------------------------------------------------- 帳號


TURNSTILE_SITEKEY = os.environ.get("DL_TURNSTILE_SITEKEY", "")
TURNSTILE_SECRET = os.environ.get("DL_TURNSTILE_SECRET", "")


# 每個欄位都要有上限。username 特別重要：它會被拿來當限流字典的 key，而那個字典
# 不淘汰 —— 沒有上限的話，未認證的請求就能用「很長的帳號名」把記憶體撐爆
# （實測 15 個 5 MB 的 username 讓 RSS 從 220 MB 漲到 324 MB 且不回收）。
Username = Annotated[str, Field(max_length=64)]
Password = Annotated[str, Field(max_length=auth.PASSWORD_MAX)]


class RegisterIn(BaseModel):
    username: Username
    password: Password
    turnstile_token: str | None = Field(default=None, max_length=4096)


class LoginIn(BaseModel):
    username: Username
    password: Password


class PasswordIn(BaseModel):
    current_password: Password
    new_password: Password


class RecoverIn(BaseModel):
    username: Username
    recovery_code: Annotated[str, Field(max_length=128)]
    new_password: Password


class DeleteAccountIn(BaseModel):
    password: Password


def _limit(limiter: ratelimit.Limiter, key: str) -> None:
    wait = limiter.check(key)
    if wait:
        raise HTTPException(
            429, "太頻繁了，稍後再試", headers={"retry-after": str(int(wait) + 1)}
        )


def _verify_turnstile(token: str | None, ip: str) -> None:
    """沒設 secret 就不啟用（本機開發與測試）。啟用與否在啟動時 log 明示，不靜默。"""
    if not TURNSTILE_SECRET:
        return
    import httpx

    try:
        r = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET, "response": token or "", "remoteip": ip},
            timeout=8,
        )
        ok = r.json().get("success") is True
    except Exception as exc:  # 驗不到就不放行 —— fail closed
        raise HTTPException(503, "驗證服務暫時無法使用，請稍後再試") from exc
    if not ok:
        raise HTTPException(400, "人機驗證未通過")


@app.post("/api/auth/register")
def register(payload: RegisterIn, request: Request):
    ip = ratelimit.client_ip(request)
    _limit(ratelimit.REGISTER_IP, ip)
    _verify_turnstile(payload.turnstile_token, ip)
    auth.validate_username(payload.username)
    auth.validate_password(payload.password)
    # 雜湊在交易外算好再進去，理由同 login
    creds = auth.new_credentials(payload.password)

    with db.session(write=True) as conn:
        user_id, code = auth.create_user(conn, payload.username, creds)
        token = auth.create_session(
            conn, user_id, ip, request.headers.get("user-agent", "")
        )
    out = JSONResponse({"username": payload.username, "recovery_code": code})
    auth.set_cookie(out, token)
    return out


@app.post("/api/auth/login")
def login(payload: LoginIn, request: Request):
    ip = ratelimit.client_ip(request)
    _limit(ratelimit.LOGIN_IP, ip)
    _limit(ratelimit.LOGIN_USER, payload.username.lower())

    # 雜湊驗證在交易「外面」。Argon2 一次 143ms，跑在 BEGIN IMMEDIATE 裡等於讓
    # 未認證的假登入獨佔整個 app 的 SQLite 寫鎖 —— 實測 40 個併發假登入會讓同時間
    # 的 /api/answer、/api/sync 全部撞上 busy_timeout 回 500。交易裡只留 INSERT。
    with db.session() as conn:
        user = auth.find_user(conn, payload.username)
    # 帳號不存在也要跑滿一次雜湊驗證。少了它，「查無此人」會比「密碼錯誤」
    # 快一個數量級，光靠計時就能把帳號列舉出來。錯誤訊息也必須完全一樣。
    if not auth.verify_password(user["password_hash"] if user else None, payload.password):
        raise HTTPException(401, "帳號或密碼不正確")
    rehashed = auth.hash_password(payload.password) if auth.needs_rehash(user["password_hash"]) else None

    with db.session(write=True) as conn:
        if rehashed:
            conn.execute("UPDATE user SET password_hash=? WHERE id=?", (rehashed, user["id"]))
        auth.purge_expired_sessions(conn, user["id"])
        token = auth.create_session(
            conn, user["id"], ip, request.headers.get("user-agent", "")
        )
        conn.execute(
            "UPDATE user SET last_seen_at=? WHERE id=?",
            (db.iso(datetime.now(timezone.utc)), user["id"]),
        )
    ratelimit.LOGIN_USER.forget(payload.username.lower())
    out = JSONResponse({"username": user["username"]})
    auth.set_cookie(out, token)
    return out


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        with db.session(write=True) as conn:
            auth.drop_session(conn, token)
    out = JSONResponse({"ok": True})
    auth.clear_cookie(out)
    return out


@app.get("/api/me")
def me(user: sqlite3.Row = CurrentUser):
    return {"username": user["username"], "created_at": user["created_at"]}


@app.post("/api/auth/password")
def change_password(payload: PasswordIn, request: Request,
                    user: sqlite3.Row = CurrentUser):
    auth.validate_password(payload.new_password)
    token = request.cookies.get(auth.COOKIE_NAME)
    with db.session() as conn:
        row = conn.execute(
            "SELECT password_hash FROM user WHERE id=?", (user["id"],)
        ).fetchone()
    if not auth.verify_password(row["password_hash"], payload.current_password):
        raise HTTPException(401, "目前的密碼不正確")
    creds = auth.new_credentials(payload.new_password)
    code = creds.recovery_code

    with db.session(write=True) as conn:
        conn.execute(
            "UPDATE user SET password_hash=?, recovery_hash=? WHERE id=?",
            (creds.password_hash, creds.recovery_hash, user["id"]),
        )
        # 換密碼多半正是因為「可能已經外洩」。只換密碼卻留著舊 session，
        # 等於什麼都沒做 —— 對方手上的 cookie 照樣有效。
        auth.drop_all_sessions(conn, user["id"], keep=token)
    return {"recovery_code": code}


@app.post("/api/auth/recover")
def recover(payload: RecoverIn, request: Request):
    ip = ratelimit.client_ip(request)
    _limit(ratelimit.RECOVER_IP, ip)
    auth.validate_password(payload.new_password)

    with db.session() as conn:
        user = auth.find_user(conn, payload.username)
    if not auth.verify_recovery_code(
        user["recovery_hash"] if user else None, payload.recovery_code
    ):
        raise HTTPException(401, "帳號或救援碼不正確")
    creds = auth.new_credentials(payload.new_password)
    code = creds.recovery_code

    with db.session(write=True) as conn:
        conn.execute(
            "UPDATE user SET password_hash=?, recovery_hash=? WHERE id=?",
            (creds.password_hash, creds.recovery_hash, user["id"]),
        )
        auth.drop_all_sessions(conn, user["id"])  # 舊救援碼用掉了，全部重來
        token = auth.create_session(
            conn, user["id"], ip, request.headers.get("user-agent", "")
        )
    out = JSONResponse({"username": user["username"], "recovery_code": code})
    auth.set_cookie(out, token)
    return out


@app.delete("/api/account")
def delete_account(payload: DeleteAccountIn, user: sqlite3.Row = CurrentUser):
    with db.session() as conn:
        row = conn.execute(
            "SELECT password_hash FROM user WHERE id=?", (user["id"],)
        ).fetchone()
    if not auth.verify_password(row["password_hash"], payload.password):
        raise HTTPException(401, "密碼不正確")

    with db.session(write=True) as conn:
        # 一行 DELETE，其餘靠 ON DELETE CASCADE。手動一張表一張表清，
        # 總有一天會漏掉新加的那張。
        conn.execute("DELETE FROM user WHERE id=?", (user["id"],))
    out = JSONResponse({"ok": True})
    auth.clear_cookie(out)
    return out


# ---------------------------------------------------------------- decks / queues


@app.get("/api/decks")
def decks(user: sqlite3.Row = CurrentUser):
    with db.session() as conn:
        uid = user["id"]
        out = []
        for key in DECKS:
            d = DECK_DATA[key]
            # 以題庫為主迴圈，不是以 DB 列為主。題庫改版是「重跑 pipeline 覆蓋 JSON、
            # 免 migration」，被刪掉的題號在 card_state 裡會留下孤兒列 —— 拿列數去
            # 減題庫總數會算出錯的未學習數，殘留夠多時甚至是負的，而且不會有任何
            # 錯誤浮上來。查無列一律當未學習，多出來的列自然就被忽略。
            st = _states(conn, uid, key)
            c = Counter(st.get(q["id"], srs.UNLEARNED) for q in d.questions)
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
            "preset": db.preset_name(conn, uid),
            "paused": db.get_setting(conn, uid, "paused", "0") == "1",
        }


@app.get("/api/queue/{kind}")
def queue(
    kind: str,
    deck: str = Query(...),
    category: str | None = None,
    shuffle: bool = False,
    limit: int = Query(0, ge=0),
    user: sqlite3.Row = CurrentUser,
):
    """三個入口（照 kg 的 Menu 結構）：unlearned / due / all。刻意沒有每日上限。"""
    if kind not in ("unlearned", "due", "all"):
        raise HTTPException(404, f"unknown queue: {kind}")
    d = _deck_or_404(deck)
    with db.session() as conn:
        uid = user["id"]
        now = _now(conn, uid)
        meta = {r["card_id"]: r for r in _rows(conn, uid, deck)}

    unlearned, due = [], []
    for q in d.questions:
        if category and q["category"] != category:
            continue
        r = meta.get(q["id"])
        if r is None:  # 沒有列 = 沒答過
            unlearned.append(q["id"])
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
def card(card_id: str, user: sqlite3.Row = CurrentUser):
    for d in DECK_DATA.values():
        q = d.by_id.get(card_id)
        if q:
            break
    else:
        raise HTTPException(404, f"unknown card: {card_id}")

    with db.session() as conn:
        uid = user["id"]
        s = db.get_state(conn, uid, card_id, _cfg(conn, uid))
        now = _now(conn, uid)
        return {
            "question": q,
            "state": srs.derive_state(s.review_count, s.next_review_at, now),
            # 沒答過就沒有複習資料可報。v1 因為開機把 1896 張卡全 seed 進 DB，
            # 這裡對從沒碰過的卡也會回一組 interval_hours=24.0 的預設值 —— 那是
            # 設定檔的值，不是關於這張卡的事實。惰性建列後一律回 null。
            "review": {
                "interval_hours": s.review_interval_hours,
                "next_review_at": db.iso(s.next_review_at),
                "review_count": s.review_count,
                "lapse_count": s.lapse_count,
                "streak": s.review_streak,
                "last_feedback": s.last_review_feedback,
            }
            if s.review_count
            else None,
            "note": _note(conn, uid, card_id),
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
    # 上限不是效能考量，是「一個請求能讓伺服器做多少事」的閘門。
    # 前端補送本來就分批（每批 500 筆），正常使用永遠碰不到。
    answers: list[AnswerIn] = Field(default_factory=list, max_length=5000)
    notes: dict[str, Annotated[str, Field(max_length=4000)]] = Field(
        default_factory=dict, max_length=5000
    )


def _question(card_id: str) -> dict:
    for d in DECK_DATA.values():
        q = d.by_id.get(card_id)
        if q:
            return q
    raise HTTPException(404, f"unknown card: {card_id}")


def _record(conn, user_id: int, q: dict, chosen: int | None, correct: bool, remembered: bool,
            elapsed_ms: int | None, before: srs.CardState,
            reviewed_at: datetime | None = None,
            client_event_id: str | None = None,
            dirty: set[tuple[str, str]] | None = None) -> dict:
    """呼叫端必須在 db.session(write=True) 之內（regrade 的 DELETE + 重打要同一交易）。"""
    now = _now(conn, user_id, reviewed_at)
    preset = db.preset_name(conn, user_id)
    cfg = srs.ReviewSettings.preset(preset)
    after = srs.apply_feedback(before, remembered, cfg, now)
    db.put_state(conn, user_id, q["id"], q["deck"], after)
    db.log_event(
        conn,
        user_id,
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
        preset=preset,  # 重算時要用「當時」的參數，不是現在的
    )

    # 亂序偵測：這張卡若已經有比 now 更晚的事件，剛剛那筆增量套用就是把舊事件
    # 疊在新狀態上，結果是錯的。回到事件日誌整段重算才會收斂。
    # dirty 不是 None 表示呼叫端要整批做完再重算（/api/sync 一批可能碰同一張卡好幾次，
    # 每次都重算是白工），此時先記下來就好。
    if db.has_later_event(conn, user_id, q["id"], now):
        if dirty is None:
            after = db.rebuild_card(conn, user_id, q["id"], q["deck"], cfg)
        else:
            dirty.add((q["id"], q["deck"]))

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


def _apply_answer(conn, user_id: int, payload: AnswerIn,
                  dirty: set[tuple[str, str]] | None = None) -> dict:
    """呼叫端必須在 db.session(write=True) 之內。"""
    q = _question(payload.card_id)  # 題號不存在於題庫就 404
    before = db.get_state(conn, user_id, payload.card_id, _cfg(conn, user_id))
    correct = payload.chosen == q["answer"]
    return _record(
        conn, user_id, q, payload.chosen, correct, correct and not payload.guessed,
        payload.elapsed_ms, before, payload.reviewed_at, payload.client_event_id, dirty,
    )


@app.post("/api/answer")
def answer(payload: AnswerIn, user: sqlite3.Row = CurrentUser):
    with db.session(write=True) as conn:
        uid = user["id"]
        if payload.client_event_id and db.event_exists(conn, uid, payload.client_event_id):
            raise HTTPException(409, "duplicate client_event_id")
        return _apply_answer(conn, uid, payload)


@app.post("/api/answer/regrade")
def regrade(payload: RegradeIn, user: sqlite3.Row = CurrentUser):
    """把「上一次作答」改判為 forgot（前端的「我是猜的」）。

    不是再送一次 answer —— 那會被當成第二次作答，從已經放大的間隔再乘 0.5，
    數字會錯（60h → 30h，而不是應有的 12h），review_count 也會多一次。
    這裡先用事件裡的 before 快照把卡片精確回捲，刪掉該事件，再以 forgot 重打一次。
    """
    q = _question(payload.card_id)
    with db.session(write=True) as conn:
        uid = user["id"]
        ev = db.latest_event(conn, uid, payload.card_id)
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
        conn.execute(
            "DELETE FROM review_event WHERE user_id=? AND id=?", (uid, ev["id"])
        )
        return _record(
            conn, uid, q, ev["chosen"], bool(ev["correct"]), False, ev["elapsed_ms"], before,
            db.parse(ev["reviewed_at"]), ev["client_event_id"],
        )


# ---------------------------------------------------------------- offline sync


def _config_payload() -> dict:
    """對所有人都一樣、而且不會變的東西。公開，可長期快取，service worker 預先抓。

    刻意不含 SRS 參數：那些由 static/srs.js 自帶（PRESETS 與 app/srs.py 逐值對應，
    tests/test_srs_parity.py 兩邊比對）。匿名模式下沒有伺服器可問，參數只能在本地；
    與其讓同一組數字有兩個來源，不如讓本地那份成為唯一來源、用測試釘住它一致。
    """
    return {
        "server_time": db.iso(srs.utcnow()),
        "presets": list(srs.PRESETS),
        "default_preset": srs.DEFAULT_PRESET,
        # 有設 sitekey 前端才渲染 widget。兩者必須一起開：只設 secret 而前端沒有
        # widget 的話，每一次註冊都會拿到「人機驗證未通過」，而且看不出為什麼。
        "turnstile_sitekey": TURNSTILE_SITEKEY or None,
        # 影片的來源。空字串＝同源的 /videos（開發與內網）；正式站指向 R2。
        # 由伺服器下發而不是寫死在 app.js：換儲存後端不必動前端，而且 SW 已經
        # 在快取 /api/config，離線時也知道該去哪（雖然離線本來就播不了影片）。
        "video_origin": VIDEO_ORIGIN,
        "decks": [
            {
                "deck": k,
                "label": DECK_DATA[k].label,
                "title": DECK_DATA[k].title,
                "version": DECK_DATA[k].version,
                "total": len(DECK_DATA[k].questions),
                "categories": DECK_DATA[k].categories,
                # 版號進 URL：題庫換版才換網址，於是 CF 邊緣與瀏覽器都能長期快取，
                # 又不會在改版後拿到舊的（見 deck_json 的 cache-control）。
                "url": f"/data/{k}.json?v={DECK_DATA[k].version}",
            }
            for k in DECKS
        ],
    }


def _state_payload(conn, user_id: int) -> dict:
    """這個帳號的東西。需要身分，不可快取。

    有了它，前端就能在完全沒有網路時自己算出 unlearned/due/reviewed、建佇列、
    跑排程 —— 伺服器只是同步的目的地，不是每翻一張卡的必要條件。
    """
    return {
        "server_time": db.iso(srs.utcnow()),
        "reference_time": db.iso(_now(conn, user_id)),  # 凍結時鐘生效時會與 server_time 不同
        "preset": db.preset_name(conn, user_id),
        "paused": db.get_setting(conn, user_id, "paused", "0") == "1",
        "paused_at": db.get_setting(conn, user_id, "paused_at", "") or None,
        "states": db.all_states(conn, user_id),
        "notes": db.all_notes(conn, user_id),
    }


@app.get("/api/config")
def config():
    return _config_payload()


@app.get("/api/health")
def health():
    """不需要認證的健康檢查。給部署腳本、監控與還原演練用。

    為什麼需要它：README 原本教人用 `curl /api/decks` 當部署後的 smoke check
    並期待 200，但那個端點掛了 CurrentUser —— 不帶 cookie 恆為 401。也就是
    那一步從來沒有真的驗到「app 起來了」，而且照抄進腳本會直接誤判成失敗。

    回的是三件事：程序活著、DB 開得起來（真的下一次查詢，不是只回 200）、
    題庫載進記憶體了。schema 版本一起回，因為部署腳本要確認就地遷移真的跑完
    ——它是一個小整數，而題庫版本與題數本來就在公開的 /api/config 裡，
    這裡沒有多洩漏任何東西，尤其沒有任何跟使用者有關的東西。

    no-store：健康狀態被 Cloudflare 或瀏覽器快取住的話，這個端點就是在說謊。
    """
    with db.session() as conn:
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
    return JSONResponse(
        {
            "ok": True,
            "schema_version": schema,
            "decks": {
                k: {"version": DECK_DATA[k].version, "questions": len(DECK_DATA[k].questions)}
                for k in DECKS
            },
        },
        headers={"cache-control": "no-store"},
    )


@app.get("/api/state")
def state(user: sqlite3.Row = CurrentUser):
    with db.session() as conn:
        return _state_payload(conn, user["id"])


@app.post("/api/sync")
def sync(payload: SyncIn, user: sqlite3.Row = CurrentUser):
    """補送離線期間累積的作答與筆記，回傳權威狀態讓前端整份覆蓋。

    照 reviewed_at 由舊到新重放 —— 順序錯了間隔就會算錯（同一張卡答兩次，
    先後不同結果不同）。已經送成功過的 client_event_id 直接跳過，不重複計分。

    整批在同一個交易裡。可預期的個別失敗（題號不存在）照樣記進 failed 並繼續，
    其餘照常寫入 —— 那種事件重送幾次都會失敗，退整批只會讓使用者永遠卡在那裡。
    只有未預期的例外才整批回捲；補送本來就靠 client_event_id 冪等，整批重送是安全的。
    """
    applied, skipped, failed = [], [], []
    ordered = sorted(
        payload.answers,
        key=lambda a: (a.reviewed_at or srs.utcnow(), a.client_event_id or ""),
    )
    _limit(ratelimit.SYNC_USER, str(user["id"]))
    dirty: set[tuple[str, str]] = set()
    with db.session(write=True) as conn:
        uid = user["id"]
        for a in ordered:
            if a.client_event_id and db.event_exists(conn, uid, a.client_event_id):
                skipped.append(a.client_event_id)
                continue
            try:
                _apply_answer(conn, uid, a, dirty)
                applied.append(a.client_event_id)
            except HTTPException as exc:
                failed.append({"client_event_id": a.client_event_id, "detail": exc.detail})

        for card_id, body in payload.notes.items():
            text = body.strip()
            if text:
                conn.execute(
                    """INSERT INTO note(user_id, card_id, body, updated) VALUES(?,?,?,?)
                       ON CONFLICT(user_id, card_id) DO UPDATE SET body=excluded.body,
                                                                   updated=excluded.updated""",
                    (uid, card_id, text, db.iso(srs.utcnow())),
                )
            else:
                conn.execute(
                    "DELETE FROM note WHERE user_id=? AND card_id=?", (uid, card_id)
                )

        # 亂序抵達的卡整段重算。放在迴圈外：同一張卡在一批裡被碰好幾次時，
        # 每碰一次重算一次是白工，而且中間那些結果反正都會被最後一次覆蓋。
        # dirty 帶著 deck 一起收：迴圈外再呼叫 _question 的話，孤兒題號的 404
        # 會炸掉整個請求並回捲所有已寫入的事件（迴圈內的 404 是被 except 接走的）。
        cfg = _cfg(conn, uid)
        for card_id, deck in sorted(dirty):
            db.rebuild_card(conn, uid, card_id, deck, cfg)

        # 刻意在寫入交易內取快照：回傳的狀態必須剛好等於這批補送的結果，
        # 出了交易再讀就可能夾雜別台裝置插進來的寫入。代價是寫鎖多握一小段。
        out = _state_payload(conn, uid)

    out["sync"] = {"applied": applied, "skipped": skipped, "failed": failed}
    return out


# ---------------------------------------------------------------- notes


class NoteIn(BaseModel):
    # 沒有上限的話，登入使用者可以無上限地把磁碟寫滿
    body: Annotated[str, Field(max_length=4000)]


@app.get("/api/note/{card_id}")
def get_note(card_id: str, user: sqlite3.Row = CurrentUser):
    with db.session() as conn:
        return {"card_id": card_id, "body": _note(conn, user["id"], card_id) or ""}


@app.put("/api/note/{card_id}")
def put_note(card_id: str, payload: NoteIn, user: sqlite3.Row = CurrentUser):
    body = payload.body.strip()
    with db.session(write=True) as conn:
        uid = user["id"]
        if body:
            conn.execute(
                """INSERT INTO note(user_id, card_id, body, updated) VALUES(?,?,?,?)
                   ON CONFLICT(user_id, card_id) DO UPDATE SET body=excluded.body,
                                                               updated=excluded.updated""",
                (uid, card_id, body, db.iso(srs.utcnow())),
            )
        else:
            conn.execute("DELETE FROM note WHERE user_id=? AND card_id=?", (uid, card_id))
    return {"card_id": card_id, "body": body}


# ---------------------------------------------------------------- browse / stats


@app.get("/api/browse")
def browse(
    deck: str = Query(...),
    q: str | None = None,
    category: str | None = None,
    wrong_only: bool = False,
    noted_only: bool = False,
    user: sqlite3.Row = CurrentUser,
):
    d = _deck_or_404(deck)
    with db.session() as conn:
        uid = user["id"]
        states = _states(conn, uid, deck)
        lapsed = {
            r["card_id"]
            for r in conn.execute(
                "SELECT card_id FROM card_state"
                " WHERE user_id=? AND deck=? AND lapse_count > 0",
                (uid, deck),
            ).fetchall()
        }
        noted = {
            r["card_id"]
            for r in conn.execute(
                "SELECT card_id FROM note WHERE user_id=?", (uid,)
            ).fetchall()
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
def stats(deck: str = Query(...), user: sqlite3.Row = CurrentUser):
    d = _deck_or_404(deck)
    with db.session() as conn:
        uid = user["id"]
        now = _now(conn, uid)

        # 14 日到期預測（今天以前全部收攏進今天，照 kg 的 forecast 做法）
        forecast: Counter[str] = Counter()
        for r in _rows(conn, uid, deck):
            # 題庫改版刪掉的題號會留下孤兒列，別讓它們出現在預測裡
            if r["review_count"] == 0 or r["card_id"] not in d.by_id:
                continue
            nxt = db.parse(r["next_review_at"])
            if nxt is None:
                continue
            day = max((nxt.date() - now.date()).days, 0)
            if day < 14:
                forecast[(now.date() + timedelta(days=day)).isoformat()] += 1

        rows = conn.execute(
            """SELECT day_key, COUNT(*) n, SUM(correct) ok FROM review_event
               WHERE user_id=? AND deck=? GROUP BY day_key ORDER BY day_key""",
            (uid, deck),
        ).fetchall()
        activity = [{"day": r["day_key"], "count": r["n"], "correct": r["ok"] or 0} for r in rows]

        # 連續天數（往回數到第一個沒作答的日子）
        days = {a["day"] for a in activity}
        streak = 0
        cursor = now.date()
        while cursor.isoformat() in days:
            streak += 1
            cursor -= timedelta(days=1)

        worst = conn.execute(
            """SELECT card_id, lapse_count FROM card_state
               WHERE user_id=? AND deck=? AND lapse_count > 0
               ORDER BY lapse_count DESC LIMIT 20""",
            (uid, deck),
        ).fetchall()
        by_cat: Counter[str] = Counter()
        for r in conn.execute(
            "SELECT card_id, lapse_count FROM card_state"
            " WHERE user_id=? AND deck=? AND lapse_count > 0",
            (uid, deck),
        ).fetchall():
            q_ = d.by_id.get(r["card_id"])
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
        "worst_cards": [
            {"id": r["card_id"], "lapses": r["lapse_count"]}
            for r in worst
            if r["card_id"] in d.by_id
        ],
        "worst_categories": [{"category": k, "lapses": v} for k, v in by_cat.most_common(10)],
    }


# ---------------------------------------------------------------- settings


class SettingsIn(BaseModel):
    preset: str | None = None
    paused: bool | None = None


@app.get("/api/settings")
def get_settings(user: sqlite3.Row = CurrentUser):
    with db.session() as conn:
        return _settings_payload(conn, user["id"])


def _settings_payload(conn, user_id: int) -> dict:
    cfg = _cfg(conn, user_id)
    return {
        "preset": db.preset_name(conn, user_id),
        "presets": list(srs.PRESETS),
        "paused": db.get_setting(conn, user_id, "paused", "0") == "1",
        "paused_at": db.get_setting(conn, user_id, "paused_at", "") or None,
        "params": {
            "initial_hours": cfg.initial_hours,
            "remembered_mult": cfg.remembered_mult,
            "forgot_mult": cfg.forgot_mult,
            "min_hours": cfg.min_hours,
            "max_hours": cfg.max_hours,
        },
    }


@app.put("/api/settings")
def put_settings(payload: SettingsIn, user: sqlite3.Row = CurrentUser):
    with db.session(write=True) as conn:
        uid = user["id"]
        if payload.preset is not None:
            if payload.preset not in srs.PRESETS:
                raise HTTPException(400, f"unknown preset: {payload.preset}")
            db.set_setting(conn, uid, "preset", payload.preset)
        if payload.paused is not None:
            if payload.paused:
                # 進入暫停時把當下時間凍住，之後所有到期計算都用它
                db.set_setting(conn, uid, "paused_at", db.iso(srs.utcnow()))
                db.set_setting(conn, uid, "paused", "1")
            else:
                db.set_setting(conn, uid, "paused", "0")
        return _settings_payload(conn, uid)


# ---------------------------------------------------------------- static


IMMUTABLE = "public, max-age=31536000, immutable"


class _Images(StaticFiles):
    """題目圖與危險感知影片：一年、immutable。

    題目圖的檔名是題號（C0001_1.png），內容綁定題號、不會就地改版 —— 題庫改版是
    換一份 JSON，圖檔要嘛沿用、要嘛換名字，所以「同一個網址的內容永遠不變」成立。

    影片更乾脆：檔名就是「原始檔 md5 ＋ 轉檔參數」的雜湊（見
    tools/transcode_hazard_videos.py），內容變了檔名一定跟著變，
    immutable 是這裡最誠實的標頭。

    這件事在公網上的份量與內網完全不同：354 張圖 1.8 MB 加上影片幾百 MB，
    每個新訪客都回原站的話走的是家用上行。給了 immutable，CF 邊緣才擋得下來。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["cache-control"] = IMMUTABLE
        return resp


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


app.mount("/images", _Images(directory=DATA_DIR / "images"), name="images")
# 影片不進 git（data/videos/ 在 .gitignore 裡，內容由 tools/fetch+transcode 重生，
# 或乾脆由 R2 供應），所以「沒跑過影片 pipeline」是全新 clone、CI 與 git worktree
# 的正常狀態。而 StaticFiles 對不存在的目錄是在建構時就 raise —— 連 import
# app.main 都會失敗（實測：99 個測試在 setup 就 error，死因是
# RuntimeError: Directory '.../data/videos' does not exist）。
#
# 建一個空目錄而不是 check_dir=False：後者只跳過建構時的檢查，請求進來照樣
# 在 stat 時 raise，變成 500 而不是 404。也不用「存在才 mount」——那會讓路由
# 表隨環境改變，測到的東西跟生產跑的東西不是同一份。
# db.init() 對 DATA_DIR 用的是同一招。
(DATA_DIR / "videos").mkdir(parents=True, exist_ok=True)
app.mount("/videos", _Images(directory=DATA_DIR / "videos"), name="videos")
app.mount("/static", _Static(directory=ROOT / "static"), name="static")


@app.get("/data/{deck}.json")
def deck_json(deck: str, v: str | None = None):
    """整份題庫。service worker 預先快取這個 URL，離線時題目直接從 Cache Storage 讀。

    兩份加起來 1.5 MB、gzip 後 179 KB —— 整包收進手機是零負擔的。

    版號對得上才給長快取：網址由 /api/config 產生（?v=<source.version>），
    題庫改版就換網址，所以「同一個網址的內容永遠不變」成立，immutable 是誠實的。
    對不上（有人自己亂填或省略）就退回 no-cache —— 給錯的東西長快取，
    等於在邊緣種下一份永遠清不掉的錯誤內容。
    """
    d = _deck_or_404(deck)
    fresh = IMMUTABLE if v == d.version else "no-cache"
    return FileResponse(
        DATA_DIR / f"{deck}_questions.json",
        media_type="application/json",
        headers={"cache-control": fresh},
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


@app.get("/privacy")
def privacy():
    """這個 app 收了什麼、沒收什麼。

    沒有 email、沒有分析、沒有第三方 script（Turnstile 沒啟用時 CSP 連
    default-src 'self' 都沒放寬）。所以這頁很短 —— 短是重點，不是偷懶。
    """
    return HTMLResponse(
        PRIVACY_HTML, headers={"cache-control": "no-cache"}
    )


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


PRIVACY_HTML = """<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>資料說明 · 駕照筆試</title>
<link rel="stylesheet" href="/static/style.css">
</head><body><main class="shell prose">
<h1>資料說明</h1>

<h2>不登入時</h2>
<p>你的作答紀錄<b>完全不會離開這台裝置</b>。題庫與排程都在瀏覽器裡跑，
伺服器一列資料都不會寫。代價是清除瀏覽資料就會消失 —— 設定頁可以匯出備份。</p>

<h2>登入之後</h2>
<p>只為了跨裝置同步而存在。伺服器會存：帳號名稱、密碼的雜湊（Argon2id，
無法還原成密碼）、救援碼的雜湊、你的作答紀錄與筆記、以及登入時的 IP 與瀏覽器
字串（用來讓你辨認自己的登入）。</p>

<h2>沒有收的東西</h2>
<p>沒有 email、沒有真實姓名、沒有電話。這個 app 本身沒有分析工具、沒有廣告、
沒有追蹤碼。</p>
<p>本站的內容安全政策（CSP）禁止載入任何第三方 script。Cloudflare 目前會在
HTML 裡插入一段自家的分析程式 —— 它會被 CSP 擋下、不會執行，也不會送出任何
資料（已實測：requests 的 transferSize 為 0）。</p>

<h2>刪除</h2>
<p>設定頁的「刪除帳號」會把上述所有資料一次刪除，不保留備份中的副本以外的東西
（異地備份最多保留 14 天後自然輪替掉）。</p>

<h2>其他</h2>
<p>網站經 Cloudflare 代理，因此 Cloudflare 會依其自身政策記錄存取紀錄
（來源 IP、網址、時間）。
題庫內容取自交通部公路局公開發布的筆試題庫 PDF。</p>

<p><a href="/">← 回到 app</a></p>
</main></body></html>"""


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html",
                        headers={"cache-control": "no-cache"})
