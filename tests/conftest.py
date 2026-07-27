"""共用 fixture。

`app.main` 在 import 時就載入題庫並開 DB（`db.init()`），DB 位置又來自 import 時
讀進 `app.db.DATA_DIR` 的環境變數 —— 所以換 DB 一定要先設 env 再 reload 兩個模組，
順序不能顛倒。四個測試檔以前各自抄一份，改動 fixture 就得改四個地方。
"""

import importlib
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient


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


@contextmanager
def _fresh_client(data_dir):
    with data_dir_env(data_dir):
        import app.db
        import app.main

        importlib.reload(app.db)
        main = importlib.reload(app.main)
        with TestClient(main.app) as c:
            yield c


@pytest.fixture()
def client(tmp_path):
    """每個測試一份全新 DB。"""
    with _fresh_client(tmp_path) as c:
        yield c


@pytest.fixture(scope="module")
def module_client(tmp_path_factory):
    """整個模組共用一份 DB —— 給刻意要讓狀態累積的端到端測試用。"""
    with _fresh_client(tmp_path_factory.mktemp("dl")) as c:
        yield c
