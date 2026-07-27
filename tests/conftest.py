"""共用 fixture。

`app.main` 在 import 時就載入題庫並開 DB（`db.init()`），DB 位置又來自 import 時
讀進 `app.db.DATA_DIR` 的環境變數 —— 所以換 DB 一定要先設 env 再 reload 兩個模組，
順序不能顛倒。四個測試檔以前各自抄一份，改動 fixture 就得改四個地方。
"""

import importlib
import os

import pytest
from fastapi.testclient import TestClient


def _fresh_client(data_dir):
    os.environ["DL_DATA_DIR"] = str(data_dir)
    import app.db
    import app.main

    importlib.reload(app.db)
    main = importlib.reload(app.main)
    return TestClient(main.app)


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
