import os
import tempfile

import pytest

# 每个测试会话独立的临时数据目录，必须在 import app 之前设置
_TMP = tempfile.mkdtemp(prefix="pantryfifo-test-")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import db_path  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    """每个用例一张全新种子库：删掉库文件重新 init。"""
    p = db_path()
    if p.exists():
        p.unlink()
    seed.init_db()
    with TestClient(app) as c:  # startup 再跑一次 init_db，幂等
        yield c
