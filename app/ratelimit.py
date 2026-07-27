"""記憶體內的速率限制。

刻意不引入 redis / slowapi：這是單一 uvicorn 程序，程序內的計數就是全部的真相。
程序重啟會清空 —— 可接受，重啟不是攻擊者能控制的事件。

擋的是「線上暴力嘗試」。離線破解（DB 外流後拿雜湊去跑字典）要靠 Argon2id 的成本，
不是靠這裡；兩者互補，缺一不可。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# cloudflared 跑在同一台機器上，所以「來源是 loopback」就等於「這個請求真的來自我們的代理」。
# 只有這種請求的 CF-Connecting-IP / X-Forwarded-For 才可信 —— 直接對外的請求可以
# 隨便偽造這些標頭，信了等於把限流關掉。
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1"})


def client_ip(request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer not in TRUSTED_PROXIES:
        return peer
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # 最右邊那一跳是我們的代理填的，左邊都可能是客戶端自己塞的
        return fwd.split(",")[-1].strip()
    return peer


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class Limiter:
    """token bucket。capacity 決定可以爆發多少次，per_seconds 決定回補速度。"""

    capacity: int
    per_seconds: float
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # 字典是攻擊者能間接控制大小的東西，所以要有上限。滿了就把最舊的那批丟掉：
    # 被丟掉的等於重新獲得配額，但要撐滿這個上限本身就得先通過 IP 維度的限流。
    MAX_KEYS = 50_000

    def check(self, key: str) -> float:
        """回傳 0 表示放行；否則回傳「還要等幾秒」。"""
        rate = self.capacity / self.per_seconds
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) >= self.MAX_KEYS:
                stale = sorted(self._buckets.items(), key=lambda kv: kv[1].updated)
                for k, _ in stale[: self.MAX_KEYS // 4]:
                    del self._buckets[k]
            b = self._buckets.get(key)
            if b is None:
                self._buckets[key] = _Bucket(self.capacity - 1, now)
                return 0.0
            b.tokens = min(self.capacity, b.tokens + (now - b.updated) * rate)
            b.updated = now
            if b.tokens >= 1:
                b.tokens -= 1
                return 0.0
            return (1 - b.tokens) / rate

    def forget(self, key: str) -> None:
        """登入成功後把該帳號的失敗計數清掉，密碼對了就不該再被自己的重試拖累。"""
        with self._lock:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# 登入：帳號維度比 IP 維度嚴，但兩者都不做「鎖定帳號」——
# 硬鎖定是送給攻擊者的 DoS：知道你的帳號就能讓你登不進去。
# 這裡只是讓每次嘗試變慢，正確的密碼一送出就 forget()。
LOGIN_IP = Limiter(capacity=20, per_seconds=60)
LOGIN_USER = Limiter(capacity=8, per_seconds=60)
# 註冊：唯一會無限增加 DB 列的入口，卡最緊
REGISTER_IP = Limiter(capacity=5, per_seconds=3600)
RECOVER_IP = Limiter(capacity=5, per_seconds=3600)
SYNC_USER = Limiter(capacity=120, per_seconds=60)

ALL = (LOGIN_IP, LOGIN_USER, REGISTER_IP, RECOVER_IP, SYNC_USER)


def reset_all() -> None:
    for limiter in ALL:
        limiter.reset()
