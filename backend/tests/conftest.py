"""pytest 公共夹具：每个用例独立临时库、还原逻辑时钟与故障注入。"""
import importlib
import sys
from pathlib import Path

import pytest

# 保证可 import app（测试目录在 backend/tests，后端根在其上一级）
后端根 = Path(__file__).resolve().parent.parent
if str(后端根) not in sys.path:
    sys.path.insert(0, str(后端根))


@pytest.fixture()
def 临时库(tmp_path, monkeypatch):
    monkeypatch.setenv("数据目录", str(tmp_path / "data"))
    monkeypatch.setenv("夹具目录", str(后端根.parent / "fixtures"))
    # 延迟到环境变量就位后再导入，确保 db.库路径 指向临时库
    from app import seed
    from app.services import 切日租约
    from app.services.时间 import 置时钟
    seed.初始化库()
    切日租约.故障钩子 = None
    置时钟(None)
    yield
    切日租约.故障钩子 = None
    置时钟(None)
