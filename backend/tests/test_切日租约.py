"""切日互斥核单元测试（口径乙：配对租约排队）。

覆盖：
- 互斥实体为库内租约/排队，禁止进程内私有锁（跨子进程可见可核证）
- FIFO 排队、核证接口（持有方/租约截止/valid）
- 租约到期失败闭、排队超时
- 无租约写入被闸门拒绝
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from app.db import 连接
from app.services import 切日租约
from app.services.时间 import 置时钟


class 假时钟:
    def __init__(self, 起步=1000.0):
        self.t = 起步

    def __call__(self):
        return self.t

    def 推进(self, 秒):
        self.t += 秒


def test_核证接口字段与空态(临时库):
    证 = 切日租约.核证()
    assert set(["持有方", "租约截止", "valid"]).issubset(证.keys())
    assert 证["valid"] is False and 证["持有方"] is None


def test_取租约后核证valid_释放后失效(临时库):
    with 切日租约.切日上下文("甲", "装载批", 租约秒=30) as 票:
        证 = 切日租约.核证()
        assert 证["valid"] is True
        assert 证["持有方"] == "甲"
        assert 证["租约截止"] > 证["取得时刻"]
        # 指定租约核证
        assert 切日租约.核证(票["租约号"])["valid"] is True
    assert 切日租约.核证()["valid"] is False


def test_租约到期即失败闭(临时库):
    钟 = 假时钟()
    置时钟(钟)
    try:
        票 = 切日租约.申请租约("到期户", "抽取批", 租约秒=1.0)
        钟.推进(5.0)  # 越过截止
        # reaper 应把它打成失败闭
        切日租约.核证()
        证 = 切日租约.核证(票["租约号"])
        assert 证["valid"] is False
        conn = 连接()
        try:
            队 = conn.execute(
                "SELECT 状态 FROM 切日排队 WHERE 排队号=?", (票["排队号"],)
            ).fetchone()
            assert 队["状态"] == "失败闭"
        finally:
            conn.close()
        # 失租核验抛 租约已失
        import pytest
        with pytest.raises(切日租约.租约已失):
            切日租约.核验(票["租约号"])
    finally:
        置时钟(None)


def test_排队超时不留写权(临时库):
    import pytest
    with 切日租约.切日上下文("长占", "装载批", 租约秒=30):
        with pytest.raises(切日租约.排队超时):
            切日租约.申请租约("久等", "抽取批", 租约秒=5, 等待秒=0.2)
        # 超时者排队行被置失败闭，且从未持有租约
        conn = 连接()
        try:
            行 = conn.execute(
                "SELECT 状态, 租约号 FROM 切日排队 WHERE 持有方='久等'"
            ).fetchone()
            assert 行["状态"] == "失败闭"
            assert 行["租约号"] == ""
        finally:
            conn.close()


def test_无租约写入被闸门拒绝(临时库):
    import pytest
    with pytest.raises(切日租约.无租约写入):
        切日租约.确保持租约(None)
    with pytest.raises(切日租约.租约已失):
        切日租约.确保持租约("不存在的租约号")


def test_续租仅持有者可用(临时库):
    import pytest
    with 切日租约.切日上下文("续租户", "回放批", 租约秒=1.0) as 票:
        原截止 = 切日租约.核证(票["租约号"])["租约截止"]
        切日租约.续租(票["租约号"], 10)
        assert 切日租约.核证(票["租约号"])["租约截止"] > 原截止
    with pytest.raises(切日租约.租约已失):
        切日租约.续租(票["租约号"], 5)


def test_互斥实体跨进程可见_非进程内私有锁(临时库):
    """关键约束：互斥凭据必须是库实体。独立子进程应能核证到父进程持有的租约。"""
    票 = 切日租约.申请租约("父进程持有", "装载批", 租约秒=30)
    脚本 = (
        "import os, json, sys;"
        "sys.path.insert(0, %r);"
        "from app.services import 切日租约;"
        "证=切日租约.核证(%r);"
        "print(json.dumps({'valid':证['valid'],'持有方':证['持有方']}, ensure_ascii=False))"
        % (str(Path(__file__).resolve().parent.parent), 票["租约号"])
    )
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, "-c", 脚本], env=env, capture_output=True, text=True, check=True
    )
    证 = json.loads(out.stdout.strip().splitlines()[-1])
    assert 证["valid"] is True
    assert 证["持有方"] == "父进程持有"
    切日租约.释放租约(票["租约号"])


def test_库路径在临时库(临时库):
    # 确认测试确实隔离在临时库，未污染开发库（路径动态随环境变量解析）
    from app import db
    assert str(db.库路径).startswith(os.environ["数据目录"])
