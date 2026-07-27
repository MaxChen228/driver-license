"""帳號系統與授權邊界。

這個檔案釘的是「公網上會被人真的去戳的東西」：帳號能不能被列舉、限流有沒有真的
擋住、A 能不能看到 B 的資料、登出後 cookie 還能不能用。這些東西壞掉不會有錯誤
訊息，只會安靜地漏。
"""

import os
import sqlite3

import pytest

from app import auth, ratelimit
from tests.conftest import TEST_PASSWORD, TEST_USER, _fresh_client, make_client

GOOD = "another-long-passphrase"

# 需要身分的端點全清單。新增端點時忘了掛 require_user，這裡就會紅。
PROTECTED = [
    ("GET", "/api/me", None),
    ("GET", "/api/decks", None),
    ("GET", "/api/bootstrap", None),
    ("GET", "/api/settings", None),
    ("GET", "/api/stats?deck=car", None),
    ("GET", "/api/browse?deck=car", None),
    ("GET", "/api/queue/all?deck=car", None),
    ("GET", "/api/card/C0001", None),
    ("GET", "/api/note/C0001", None),
    ("PUT", "/api/note/C0001", {"body": "x"}),
    ("PUT", "/api/settings", {"preset": "relaxed"}),
    ("POST", "/api/answer", {"card_id": "C0001", "chosen": 1}),
    ("POST", "/api/answer/regrade", {"card_id": "C0001"}),
    ("POST", "/api/sync", {"answers": [], "notes": {}}),
    ("DELETE", "/api/account", {"password": TEST_PASSWORD}),
]


# ---------------------------------------------------------------- 授權邊界


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_every_state_endpoint_requires_a_session(anon_client, method, path, body):
    r = anon_client.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} 沒有擋住未登入的請求"


@pytest.mark.parametrize("path", [
    "/", "/sw.js", "/manifest.webmanifest", "/static/app.js",
    "/data/car.json", "/api/image-manifest",
])
def test_the_app_shell_stays_public(anon_client, path):
    """匿名使用者要能完整刷題，所以殼與題庫一律公開 —— 只有伺服器端狀態要身分。"""
    assert anon_client.get(path).status_code == 200


def test_accounts_cannot_see_each_other(tmp_path_factory):
    """同一張卡、兩個帳號，資料必須完全不相通。"""
    with _fresh_client(tmp_path_factory.mktemp("iso")) as a:
        a.post("/api/answer", json={"card_id": "C0001", "chosen": 1})
        a.put("/api/note/C0001", json={"body": "alice 的筆記"})
        a.put("/api/settings", json={"preset": "intensive"})

        a.post("/api/auth/logout")
        a.post("/api/auth/register", json={"username": "bob", "password": GOOD})

        assert a.get("/api/bootstrap").json()["states"] == []
        assert a.get("/api/bootstrap").json()["notes"] == {}
        assert a.get("/api/settings").json()["preset"] == "relaxed"
        assert a.get("/api/card/C0001").json()["note"] is None
        assert a.get("/api/stats", params={"deck": "car"}).json()["answered_total"] == 0


def test_the_same_client_event_id_can_exist_in_two_accounts(tmp_path_factory):
    """冪等鍵是帳號範圍的。全域唯一的話，B 的作答會被當成 A 的重送而消失。"""
    ev = {"card_id": "C0001", "chosen": 1, "client_event_id": "same-id"}
    with _fresh_client(tmp_path_factory.mktemp("dup")) as c:
        assert c.post("/api/sync", json={"answers": [ev]}).json()["sync"]["applied"] == ["same-id"]
        c.post("/api/auth/logout")
        c.post("/api/auth/register", json={"username": "bob", "password": GOOD})
        r = c.post("/api/sync", json={"answers": [ev]}).json()
        assert r["sync"]["applied"] == ["same-id"]  # 不是 skipped
        assert len(r["states"]) == 1


# ---------------------------------------------------------------- 註冊 / 登入


def test_register_returns_a_recovery_code_once(anon_client):
    r = anon_client.post("/api/auth/register", json={"username": "amy", "password": GOOD})
    assert r.status_code == 200
    code = r.json()["recovery_code"]
    assert code.startswith("dl_") and len(code.replace("-", "")) > 20
    # 之後任何地方都不該再吐出救援碼
    assert "recovery_code" not in anon_client.get("/api/me").json()


def test_duplicate_username_is_rejected_case_insensitively(anon_client):
    anon_client.post("/api/auth/register", json={"username": "amy", "password": GOOD})
    r = anon_client.post("/api/auth/register", json={"username": "AMY", "password": GOOD})
    assert r.status_code == 409


def test_weak_and_breached_passwords_are_refused(anon_client):
    short = anon_client.post("/api/auth/register", json={"username": "amy", "password": "short"})
    assert short.status_code == 400
    # NIST 的做法：不要求大小寫數字符號，改為比對已知外洩清單
    common = anon_client.post(
        "/api/auth/register", json={"username": "amy", "password": "qwertyuiop"}
    )
    assert common.status_code == 400 and "外洩" in common.json()["detail"]


def test_bad_password_and_missing_account_are_indistinguishable(client):
    """回應必須逐字相同。差一個字就是一個帳號列舉的洞。"""
    wrong = client.post("/api/auth/login",
                        json={"username": TEST_USER, "password": "wrong-but-long-enough"})
    nobody = client.post("/api/auth/login",
                         json={"username": "nosuchperson", "password": "wrong-but-long-enough"})
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()


def test_login_issues_a_hardened_cookie(anon_client):
    anon_client.post("/api/auth/register", json={"username": "amy", "password": GOOD})
    r = anon_client.post("/api/auth/login", json={"username": "amy", "password": GOOD})
    raw = r.headers["set-cookie"]
    # __Host- 前綴會讓瀏覽器強制 Secure + Path=/ 且不得帶 Domain，
    # 順帶擋掉「從子網域寫一個同名 cookie 蓋過來」
    assert raw.startswith("__Host-dl_session=")
    low = raw.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low
    assert "Domain=" not in raw


def test_session_token_is_never_stored_in_the_clear(client, tmp_path):
    """DB 被讀走也不該能拿去當 cookie 用。"""
    conn = sqlite3.connect(tmp_path / "app.db")
    stored = conn.execute("SELECT token_sha256 FROM session").fetchall()
    assert len(stored) == 1
    token = client.cookies["__Host-dl_session"]
    assert stored[0][0] != token
    assert stored[0][0] == auth._token_hash(token)


def test_logout_kills_the_session_server_side(client):
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401


def test_a_forged_cookie_is_rejected(anon_client):
    anon_client.cookies.set("__Host-dl_session", "not-a-real-token")
    assert anon_client.get("/api/me").status_code == 401


# ---------------------------------------------------------------- 改密碼 / 救援


def test_changing_password_revokes_other_sessions(tmp_path_factory):
    """換密碼多半正是因為可能外洩。只換密碼卻留著舊 session 等於什麼都沒做。"""
    with _fresh_client(tmp_path_factory.mktemp("pw")) as phone:
        import app.main

        # 第二台裝置：另一個 client，同一個 DB、各自的 cookie jar
        with make_client(app.main) as laptop:
            laptop.post("/api/auth/login",
                        json={"username": TEST_USER, "password": TEST_PASSWORD})
            assert laptop.get("/api/me").status_code == 200

            phone.post("/api/auth/password",
                       json={"current_password": TEST_PASSWORD, "new_password": GOOD})

            assert laptop.get("/api/me").status_code == 401   # 另一台被踢掉
            assert phone.get("/api/me").status_code == 200    # 動手的這台留著


def test_changing_password_needs_the_current_one(client):
    r = client.post("/api/auth/password",
                    json={"current_password": "not-the-password", "new_password": GOOD})
    assert r.status_code == 401


def test_recovery_code_works_once_and_then_rotates(anon_client):
    code = anon_client.post(
        "/api/auth/register", json={"username": "amy", "password": GOOD}
    ).json()["recovery_code"]
    anon_client.post("/api/auth/logout")

    r = anon_client.post("/api/auth/recover", json={
        "username": "amy", "recovery_code": code, "new_password": "recovered-passphrase",
    })
    assert r.status_code == 200
    new_code = r.json()["recovery_code"]
    assert new_code != code

    # 舊碼作廢
    assert anon_client.post("/api/auth/recover", json={
        "username": "amy", "recovery_code": code, "new_password": "yet-another-phrase",
    }).status_code == 401
    # 新密碼可用
    anon_client.post("/api/auth/logout")
    assert anon_client.post("/api/auth/login", json={
        "username": "amy", "password": "recovered-passphrase",
    }).status_code == 200


def test_recovery_code_is_forgiving_about_transcription(anon_client):
    """使用者是從紙上抄回來的，大小寫與分隔線不該影響結果。"""
    code = anon_client.post(
        "/api/auth/register", json={"username": "amy", "password": GOOD}
    ).json()["recovery_code"]
    messy = code.lower().replace("-", " ").replace("dl_", "")
    assert anon_client.post("/api/auth/recover", json={
        "username": "amy", "recovery_code": messy, "new_password": "recovered-passphrase",
    }).status_code == 200


# ---------------------------------------------------------------- 刪帳號


def test_deleting_an_account_takes_everything_with_it(client, tmp_path):
    client.post("/api/answer", json={"card_id": "C0001", "chosen": 1})
    client.put("/api/note/C0001", json={"body": "筆記"})
    client.put("/api/settings", json={"preset": "intensive"})

    assert client.request("DELETE", "/api/account",
                          json={"password": TEST_PASSWORD}).status_code == 200

    conn = sqlite3.connect(tmp_path / "app.db")
    for table in ("user", "session", "card_state", "review_event", "note", "user_setting"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_deleting_an_account_needs_the_password(client):
    r = client.request("DELETE", "/api/account", json={"password": "nope-not-this-one"})
    assert r.status_code == 401


# ---------------------------------------------------------------- CSRF / 限流


def test_writes_without_the_client_header_are_refused(client):
    r = client.post("/api/sync", json={"answers": []}, headers={"X-DL-Client": ""})
    assert r.status_code == 403
    # 讀取不受影響
    assert client.get("/api/me", headers={"X-DL-Client": ""}).status_code == 200


def test_login_attempts_are_rate_limited(client):
    ratelimit.reset_all()
    codes = [
        client.post("/api/auth/login",
                    json={"username": TEST_USER, "password": "wrong-but-long-enough"}).status_code
        for _ in range(30)
    ]
    assert 429 in codes, "暴力嘗試沒有被擋下來"
    assert codes.index(429) > 1  # 但也不能一開始就擋，正常打錯一次要能重試


def test_a_correct_password_clears_the_account_penalty(client):
    ratelimit.reset_all()
    for _ in range(5):
        client.post("/api/auth/login",
                    json={"username": TEST_USER, "password": "wrong-but-long-enough"})
    assert client.post("/api/auth/login",
                       json={"username": TEST_USER, "password": TEST_PASSWORD}).status_code == 200
    # 密碼對了就不該再被自己剛才的重試拖累
    assert ratelimit.LOGIN_USER.check(TEST_USER) == 0


def test_registration_is_rate_limited_per_ip(anon_client):
    ratelimit.reset_all()
    codes = []
    for i in range(8):
        codes.append(anon_client.post(
            "/api/auth/register", json={"username": f"user{i}", "password": GOOD}
        ).status_code)
        anon_client.cookies.clear()
    assert codes[0] == 200
    assert 429 in codes, "註冊是唯一會無限增加 DB 列的入口，必須卡得住"


def test_sync_payload_size_is_capped(client):
    """一個請求能讓伺服器做多少事要有上限。前端補送本來就分批。"""
    huge = [{"card_id": "C0001", "chosen": 1, "client_event_id": f"e{i}"}
            for i in range(5001)]
    assert client.post("/api/sync", json={"answers": huge}).status_code == 422


# ---------------------------------------------------------------- 成本參數


def test_password_hashing_cost_is_pinned():
    """conftest 在測試時把 Argon2 調到最便宜，所以正式參數要在這裡單獨釘住。

    參數被誤調小不會有任何症狀 —— 直到 DB 外流、密碼被離線跑出來為止。
    """
    import importlib

    real = importlib.reload(__import__("app.auth", fromlist=["auth"]))
    ph = real._hasher
    assert ph.memory_cost == 131072   # 128 MiB
    assert ph.time_cost == 4
    assert ph.parallelism == 2
    assert real.PASSWORD_MIN == 10


def test_prod_mode_hides_the_api_schema(tmp_path):
    """關掉 /api/docs 還不夠：/openapi.json 是另一條路，而且那份更有用。"""
    import importlib

    from tests.conftest import data_dir_env, make_client

    os.environ["DL_ENV"] = "prod"
    try:
        with data_dir_env(tmp_path):
            import app.db
            import app.main

            importlib.reload(app.db)
            main = importlib.reload(app.main)
            with make_client(main) as c:
                assert c.get("/api/docs").status_code == 404
                assert c.get("/openapi.json").status_code == 404
                assert c.get("/").status_code == 200
    finally:
        del os.environ["DL_ENV"]
        importlib.reload(__import__("app.main", fromlist=["main"]))
