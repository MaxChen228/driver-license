"""帳號：註冊、登入、session、救援碼。

為什麼自建而不是接 Google／OAuth：這個 app 是 PWA，而 PWA 裡的 OAuth 轉址在
iOS 上會離開 standalone 情境（out-of-scope 導覽跑到 Safari，回來時不一定回得到
原本那個 app），是有名的坑。自建的表單登入沒有轉址，沒有第三方來源，
CSP 可以維持 default-src 'self'。

為什麼沒有 email：不收 email 就不必架 SMTP／SES，不必處理投遞率，也不必保管
任何個資。代價是沒有「寄信重設密碼」，改用註冊時一次性顯示的救援碼。

威脅模型分工：
  - 線上暴力嘗試 → ratelimit.py（每次嘗試變慢，不鎖帳號）
  - 離線破解（DB 外流）→ Argon2id 的記憶體成本
  - session 竊取 → token 只存 SHA-256、cookie HttpOnly + __Host- 前綴
  - CSRF → SameSite=Lax + 不開 CORS + 寫入一律要求 X-DL-Client 標頭
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from . import db

# m=128MiB t=4 p=2 → felix（M3 Air）上約 143ms。
# 沒有取更高的記憶體成本是因為這台機器同時在跑 Xcode build，而限流已經把並發壓住；
# 128 MiB 仍是 OWASP 次要建議值（19 MiB）的六倍以上。
_hasher = PasswordHasher(memory_cost=131072, time_cost=4, parallelism=2)



class _Argon2Gate:
    """同時最多幾個 Argon2 在算，外加一條有上限的等候線；滿了直接 503。

    限流管的是「每個來源多快」，管不到「全部來源加起來同時幾個」。每次雜湊吃
    128 MiB，而 sync 端點跑在 anyio 的 40 條共用執行緒上 —— 實測 400 個偽造 IP
    的假登入讓 RSS 衝到 5.1 GB，同時間一個匿名 GET /api/config 等了 45 秒。
    這道閘把兩件事都封頂：記憶體 ≤ slots × 128 MiB，佔用的執行緒 ≤ slots + queue，
    其餘請求照常有執行緒可用。滿了回 503 而不是排隊，是因為排隊本身就在佔執行緒。
    """

    def __init__(self, slots: int, queue: int, wait_seconds: float):
        self._slots = threading.BoundedSemaphore(slots)
        self._capacity = slots + queue
        self._wait = wait_seconds
        self._inside = 0  # 正在算 + 正在等
        self._lock = threading.Lock()

    @contextmanager
    def slot(self):
        with self._lock:
            if self._inside >= self._capacity:
                raise _busy()
            self._inside += 1
        try:
            if not self._slots.acquire(timeout=self._wait):
                raise _busy()
            try:
                yield
            finally:
                self._slots.release()
        finally:
            with self._lock:
                self._inside -= 1


def _busy() -> HTTPException:
    return HTTPException(503, "伺服器忙碌中，請稍後再試", headers={"retry-after": "5"})


# felix 是 M3（8 核），parallelism=2 → 4 個並行雜湊吃滿 8 核、512 MiB。
ARGON2_GATE = _Argon2Gate(slots=4, queue=8, wait_seconds=5)

# 帳號不存在時也要跑一次驗證，否則「查無此人」會比「密碼錯誤」快一個數量級，
# 光靠計時就能把帳號列舉出來。這個雜湊的明文永遠不會有人猜中。
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
PASSWORD_MIN, PASSWORD_MAX = 10, 128

COOKIE_NAME = "__Host-dl_session"
SESSION_DAYS = 90

_COMMON_PASSWORDS_FILE = Path(__file__).resolve().parent / "data" / "common-passwords.txt.gz"


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """SecLists top-1M 裡長度 10-128 的那些（短的本來就過不了長度閘，留著只佔記憶體）。

    NIST SP 800-63B 要求比對「已知外洩／常見」的密碼，而不是要求大小寫數字符號 ——
    組合規則只會逼出 Password1! 這種既難記又好猜的東西。
    """
    raw = gzip.decompress(_COMMON_PASSWORDS_FILE.read_bytes()).decode("utf-8")
    return frozenset(raw.split("\n"))


# ---------------------------------------------------------------- 密碼


def validate_username(username: str) -> str:
    if not USERNAME_RE.match(username or ""):
        raise HTTPException(400, "帳號需為 3-32 個字元，限英數字、底線、減號")
    return username


def validate_password(password: str) -> str:
    if not PASSWORD_MIN <= len(password or "") <= PASSWORD_MAX:
        raise HTTPException(400, f"密碼長度需在 {PASSWORD_MIN}-{PASSWORD_MAX} 之間")
    if password in _common_passwords():
        raise HTTPException(400, "這組密碼在已知的外洩清單裡，換一組")
    return password


def hash_password(password: str) -> str:
    with ARGON2_GATE.slot():
        return _hasher.hash(password)


def _verify(stored_hash: str | None, secret: str) -> bool:
    """stored_hash 為 None（尚未設密碼的帳號）也要跑滿一次雜湊，時間不能有差別。"""
    try:
        with ARGON2_GATE.slot():
            _hasher.verify(stored_hash or _DUMMY_HASH, secret)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return stored_hash is not None


def verify_password(stored_hash: str | None, password: str) -> bool:
    return _verify(stored_hash, password)


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# ---------------------------------------------------------------- 救援碼


def new_recovery_code() -> str:
    """128 bit，base32 分組。只在產生的當下顯示一次，之後只剩雜湊。"""
    raw = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")
    return "dl_" + "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))


def normalize_recovery_code(code: str) -> str:
    """使用者是從紙上抄回來的：大小寫、分隔線、空白都不該影響結果。"""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper().removeprefix("DL_"))


def hash_recovery_code(code: str) -> str:
    with ARGON2_GATE.slot():
        return _hasher.hash(normalize_recovery_code(code))


def verify_recovery_code(stored_hash: str | None, code: str) -> bool:
    return _verify(stored_hash, normalize_recovery_code(code))


# ---------------------------------------------------------------- session


def _token_hash(token: str) -> str:
    """token 本身是 256 bit 隨機值，猜不到，所以不需要 pepper 或慢雜湊 ——
    SHA-256 在這裡的作用只是「DB 被讀走也偷不到可用的 cookie」。
    """
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int, ip: str, ua: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO session(token_sha256, user_id, created_at, expires_at,
                               last_used_at, ip, user_agent)
           VALUES(?,?,?,?,?,?,?)""",
        (
            _token_hash(token), user_id, db.iso(now),
            db.iso(now + timedelta(days=SESSION_DAYS)), db.iso(now), ip, ua[:200],
        ),
    )
    return token


def user_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        """SELECT u.id, u.username, u.created_at, s.expires_at
           FROM session s JOIN user u ON u.id = s.user_id
           WHERE s.token_sha256 = ?""",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    expires = db.parse(row["expires_at"])
    if expires is None or expires <= datetime.now(timezone.utc):
        return None
    return row


def purge_expired_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """順手清掉這個帳號已過期的 session 列。

    過期在 user_for_token 是「查到但判定無效」，列不會自己消失 —— 每次登入一列、
    90 天內只增不減。掛在登入這條路上清是最省事的：反正已經在寫入交易裡了。
    """
    conn.execute(
        "DELETE FROM session WHERE user_id=? AND expires_at<=?",
        (user_id, db.iso(datetime.now(timezone.utc))),
    )


def drop_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token_sha256=?", (_token_hash(token),))


def drop_all_sessions(conn: sqlite3.Connection, user_id: int, keep: str | None = None) -> None:
    """改密碼／用救援碼之後必須撤掉其他 session。

    密碼會被換掉，多半正是因為「可能已經外洩」；只換密碼而留著舊 session，
    等於什麼都沒做 —— 攻擊者手上的 cookie 照樣有效。
    """
    if keep:
        conn.execute(
            "DELETE FROM session WHERE user_id=? AND token_sha256<>?",
            (user_id, _token_hash(keep)),
        )
    else:
        conn.execute("DELETE FROM session WHERE user_id=?", (user_id,))


def set_cookie(response: Response, token: str) -> None:
    """__Host- 前綴會讓瀏覽器強制要求 Secure、Path=/ 且不得帶 Domain ——
    也就順便擋掉「從某個子網域寫一個同名 cookie 蓋過來」這種攻擊。
    """
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True, secure=True, samesite="lax", path="/",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")


# ---------------------------------------------------------------- 使用者


def find_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM user WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()


@dataclass(frozen=True)
class Credentials:
    """雜湊好的密碼與救援碼。

    刻意做成「先算好再進交易」：Argon2 一次 143ms，跑在 BEGIN IMMEDIATE 裡等於
    讓一次登入嘗試獨佔整個 app 的 SQLite 寫鎖。分開之後交易裡只剩 INSERT/UPDATE。
    """

    password_hash: str
    recovery_hash: str
    recovery_code: str


def new_credentials(password: str) -> Credentials:
    code = new_recovery_code()
    return Credentials(hash_password(password), hash_recovery_code(code), code)


def create_user(conn: sqlite3.Connection, username: str, creds: Credentials) -> tuple[int, str]:
    """回傳 (user_id, 救援碼明文)。救援碼只有這一刻拿得到。"""
    try:
        cur = conn.execute(
            """INSERT INTO user(username, password_hash, recovery_hash, created_at)
               VALUES(?,?,?,?)""",
            (username, creds.password_hash, creds.recovery_hash,
             db.iso(datetime.now(timezone.utc))),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "這個帳號已經有人用了") from None
    return cur.lastrowid, creds.recovery_code


def require_user(request: Request) -> sqlite3.Row:
    """未登入一律 401。

    刻意用獨立的唯讀連線驗身分，而不是把 conn 傳進來：驗證是唯讀的，
    綁進端點的寫入交易只會讓寫鎖握得更久。SQLite 開連線是微秒級的事。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "尚未登入")
    with db.session() as conn:
        user = user_for_token(conn, token)
    if user is None:
        raise HTTPException(401, "登入已失效，請重新登入")
    return user
