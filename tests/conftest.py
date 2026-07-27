"""共用 fixture。

`app.main` 在 import 時就載入題庫並開 DB（`db.init()`），DB 位置又來自 import 時
讀進 `app.db.DATA_DIR` 的環境變數 —— 所以換 DB 一定要先設 env 再 reload 兩個模組，
順序不能顛倒。四個測試檔以前各自抄一份，改動 fixture 就得改四個地方。

base_url 用 https 是必要的，不是講究：session cookie 帶 __Host- 前綴與 Secure，
httpx 的 cookie jar 不會把 Secure cookie 送到 http 來源，整套測試會全部 401。
"""

import importlib
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

TEST_USER = "tester"
TEST_PASSWORD = "a-long-enough-passphrase"


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """把 Argon2 調到最便宜。

    正式參數（m=128MiB t=4）一次雜湊 ~143ms，而幾乎每個 fixture 都要註冊一個帳號，
    整套測試會從 1 秒變成 12 秒。成本參數本身由 test_auth.py 單獨釘住，
    這裡換掉的只是「跑多快」，不是「怎麼算」。
    """
    from argon2 import PasswordHasher

    import app.auth

    monkeypatch.setattr(
        app.auth, "_hasher", PasswordHasher(memory_cost=8, time_cost=1, parallelism=1)
    )


@contextmanager
def data_dir_env(data_dir):
    """把 DL_DATA_DIR 指到臨時目錄，離開時還原。

    DB 路徑是每次 db.session() 呼叫 db_path() 現查的，不是 import 時綁死 ——
    不還原的話，一個還活著的 client 會在後面的模組改掉環境變數之後，
    安靜地寫到別人的 DB 去。
    """
    prev = os.environ.get("DL_DATA_DIR")
    os.environ["DL_DATA_DIR"] = str(data_dir)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("DL_DATA_DIR", None)
        else:
            os.environ["DL_DATA_DIR"] = prev


def make_client(main):
    """X-DL-Client 是所有寫入端點都要求的 CSRF 標頭，前端由 api() 統一帶上。"""
    return TestClient(
        main.app, base_url="https://testserver", headers={"X-DL-Client": "1"}
    )


@contextmanager
def _fresh_client(data_dir, *, sign_in=True):
    with data_dir_env(data_dir):
        import app.db
        import app.main
        import app.ratelimit

        importlib.reload(app.db)
        main = importlib.reload(app.main)
        # 限流是程序內的全域狀態，不跟著 DB 重建 —— 不清的話測試之間會互相汙染，
        # 而且是「跑單檔綠、跑整套紅」這種最難查的形狀。
        app.ratelimit.reset_all()
        with make_client(main) as c:
            if sign_in:
                r = c.post(
                    "/api/auth/register",
                    json={"username": TEST_USER, "password": TEST_PASSWORD},
                )
                assert r.status_code == 200, r.text
            yield c


@pytest.fixture()
def client(tmp_path):
    """每個測試一份全新 DB，已註冊並登入。"""
    with _fresh_client(tmp_path) as c:
        yield c


@pytest.fixture()
def anon_client(tmp_path):
    """沒登入的 client —— 用來驗授權邊界。"""
    with _fresh_client(tmp_path, sign_in=False) as c:
        yield c


@pytest.fixture(scope="module")
def module_client(tmp_path_factory):
    """整個模組共用一份 DB —— 給刻意要讓狀態累積的端到端測試用。"""
    with _fresh_client(tmp_path_factory.mktemp("dl")) as c:
        yield c
