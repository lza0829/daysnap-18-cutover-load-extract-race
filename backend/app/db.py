import os
import sqlite3
from pathlib import Path


def _数据目录() -> Path:
    return Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))


def _夹具目录() -> Path:
    return Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))


# 动态属性：环境变量可在进程内（测试隔离）或子进程启动时改变，路径每次现算
def __getattr__(名):
    if 名 == "数据目录":
        return _数据目录()
    if 名 == "夹具目录":
        return _夹具目录()
    if 名 == "库路径":
        return _数据目录() / "日镜账.db"
    raise AttributeError(名)


def 连接() -> sqlite3.Connection:
    """业务连接。WAL 允许排队方在持有方写事务期间继续轮询核证，busy_timeout 避免瞬时撞锁。"""
    目录 = _数据目录()
    目录.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(目录 / "日镜账.db", timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA busy_timeout = 5000")
    return c
