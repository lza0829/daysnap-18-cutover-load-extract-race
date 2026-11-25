"""装载/抽取/切点/流水/快照/回放/差额 协同测试。

口径分叉断言（全局唯一）：乙=配对租约排队，双成功允许但须守恒、无跨日半笔。
若架构被改为甲（互斥、双成功禁止），test_口径分叉_乙允许双成功 立即失败。
"""
import threading
import time

import pytest

from app.db import 连接
from app.modules import 跨日切点策略
from app.services import 切日租约, 装载, 抽取, 回放, 切日切点, 切日夹具, 快照回滚
from app.services.时间 import 置时钟


class 假时钟:
    def __init__(self, 起步=1000.0):
        self.t = 起步

    def __call__(self):
        return self.t

    def 推进(self, 秒):
        self.t += 秒


def 等持有方(持有方, 超时=5.0):
    限 = time.monotonic() + 超时
    while time.monotonic() < 限:
        证 = 切日租约.核证()
        if 证.get("持有方") == 持有方 and 证.get("valid"):
            return 证
        time.sleep(0.01)
    raise TimeoutError(f"等待 {持有方} 持租约超时")


def 流水(号, 户, 日, 额):
    return {"流水号": 号, "账户键": 户, "营业日": 日,
            "发生时": f"{日}T09:00:00Z", "变动额": 额, "摘要": "测"}


# --------------------------------------------------------------------------
# 口径分叉：乙允许双成功；同票列（租约号）落库
# --------------------------------------------------------------------------
def test_口径分叉_乙允许双成功():
    assert 切日租约.双成功允许 is True
    assert 切日租约.口径 == "配对租约排队"
    assert 跨日切点策略.双成功允许 is True
    assert 跨日切点策略.启用 is True


def test_装载抽取回放同票带租约号(临时库):
    日 = "2026-11-20"
    装 = 装载.装载流水([流水("t1", "户甲", 日, 1000)], "批-装")
    assert "租约号" in 装 and 装["租约号"]
    抽 = 抽取.日终抽取(日, "批-抽")
    assert 抽["租约号"]
    回 = 回放.回放核对(日, "批-回")
    assert 回["租约号"] and 回["通过"] is True
    conn = 连接()
    try:
        assert conn.execute(
            "SELECT 租约号 FROM 装载批次 WHERE 编号='批-装'").fetchone()["租约号"] == 装["租约号"]
        assert conn.execute(
            "SELECT 租约号 FROM 抽取批次 WHERE 编号='批-抽'").fetchone()["租约号"] == 抽["租约号"]
        assert conn.execute(
            "SELECT 租约号 FROM 周期快照 WHERE 账户键='户甲' AND 营业日=?", (日,)
        ).fetchone()["租约号"] == 抽["租约号"]
    finally:
        conn.close()


def test_并行装载与抽取_乙口径双成功且守恒(临时库):
    日 = "2026-11-20"
    果 = {}

    def 装():
        try:
            果["装"] = 装载.装载流水(
                [流水("p1", "户甲", 日, 4000), 流水("p2", "户乙", 日, 2000)],
                "并行装", 持有方="并行装载")
        except Exception as e:  # noqa: BLE001
            果["装异"] = str(e)

    def 抽():
        try:
            果["抽"] = 抽取.日终抽取(日, "并行抽", 持有方="并行抽取")
        except Exception as e:  # noqa: BLE001
            果["抽异"] = str(e)

    t1 = threading.Thread(target=装)
    t1.start()
    等持有方("并行装载")
    t2 = threading.Thread(target=抽)
    t2.start()
    t1.join(10); t2.join(10)

    # 乙口径：两侧最终都成功（甲口径此处必失败）
    assert "装异" not in 果 and "抽异" not in 果
    assert 果["装"]["批次编号"] and 果["抽"]["批次编号"]
    回 = 回放.回放核对(日, "并行回")
    assert 回["通过"] is True  # 双成功但守恒、无跨日半笔
    assert 切日租约.核证()["valid"] is False


def test_抽取租约到期_快照整体回滚无半截镜(临时库):
    日 = "2026-11-20"
    装载.装载流水([流水("e1", "户甲", 日, 100)], "先装")
    钟 = 假时钟()
    置时钟(钟)
    短 = 0.1
    切日租约.故障钩子 = lambda 租约号, 进度: 钟.推进(短 + 1) if 进度 == 1 else None
    try:
        with pytest.raises(切日租约.租约已失):
            抽取.日终抽取(日, "半截抽", 持有方="半截抽取", 租约秒=短)
    finally:
        切日租约.故障钩子 = None
        置时钟(None)
    切日租约.核证()  # 驱动 reaper
    conn = 连接()
    try:
        # 当日无任何有效快照（整批回滚，无半截日镜）
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 周期快照 WHERE 营业日=?", (日,)).fetchone()["c"] == 0
        行 = conn.execute(
            "SELECT 状态 FROM 抽取批次 WHERE 编号='半截抽'").fetchone()
        assert 行["状态"] == "失败闭"
        队 = conn.execute(
            "SELECT 状态 FROM 切日排队 WHERE 持有方='半截抽取'").fetchone()
        assert 队["状态"] == "失败闭"
    finally:
        conn.close()


def test_装载租约到期_多户已写回滚无半笔(临时库):
    日 = "2026-11-21"
    事件 = [流水("m1", "户甲", 日, 11), 流水("m2", "户乙", 日, 22), 流水("m3", "户丙", 日, 33)]
    钟 = 假时钟()
    置时钟(钟)
    短 = 0.1
    切日租约.故障钩子 = lambda 租约号, 进度: 钟.推进(短 + 1) if 进度 == 1 else None
    try:
        with pytest.raises(切日租约.租约已失):
            装载.装载流水(事件, "半截装", 持有方="半截装载", 租约秒=短)
    finally:
        切日租约.故障钩子 = None
        置时钟(None)
    切日租约.核证()
    conn = 连接()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 变动流水 WHERE 批次编号='半截装'").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT 状态 FROM 装载批次 WHERE 编号='半截装'").fetchone()["状态"] == "失败闭"
    finally:
        conn.close()


def test_切点持租约_第二路抽取排队后有序成功(临时库):
    日 = "2026-11-22"
    装载.装载流水([流水("q1", "户甲", 日, 500)], "排队装")
    果 = {}

    def 切():
        try:
            果["切"] = 切日切点.执行切点(日, 持有方="切日调度", 持有时长秒=0.3)
        except Exception as e:  # noqa: BLE001
            果["切异"] = str(e)

    def 抽():
        try:
            果["抽"] = 抽取.日终抽取(日, "二路抽", 持有方="第二路抽取")
        except Exception as e:  # noqa: BLE001
            果["抽异"] = str(e)

    t1 = threading.Thread(target=切)
    t1.start()
    等持有方("切日调度")
    t2 = threading.Thread(target=抽)
    t2.start()
    t1.join(10); t2.join(10)

    assert "切异" not in 果 and "抽异" not in 果
    assert 果["切"]["状态"] == "成功" and 果["抽"]["批次编号"]
    conn = 连接()
    try:
        抽号 = conn.execute(
            "SELECT 排队号 FROM 切日排队 WHERE 持有方='第二路抽取'").fetchone()["排队号"]
        切号 = conn.execute(
            "SELECT 排队号 FROM 切日排队 WHERE 持有方='切日调度'").fetchone()["排队号"]
        assert 抽号 > 切号  # FIFO：抽取后到，排队号更大，未无锁闯入
        assert conn.execute(
            "SELECT 状态 FROM 切日排队 WHERE 排队号=?", (抽号,)).fetchone()["状态"] == "完成"
    finally:
        conn.close()


def test_迟到补抽无租约不得绕锁写入(临时库):
    # 服务层闸门：无有效租约一律拒绝
    with pytest.raises(切日租约.无租约写入):
        切日租约.确保持租约(None)
    # 所有经服务落库的业务批次都必须带同票租约号（无租约只靠私有锁的写不存在）
    装载.装载流水([流水("l1", "户甲", "2026-11-23", 9)], "合规装")
    conn = 连接()
    try:
        assert conn.execute(
            "SELECT 租约号 FROM 装载批次 WHERE 编号='合规装'").fetchone()["租约号"] != ""
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 装载批次 WHERE 租约号=''").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 抽取批次 WHERE 租约号=''").fetchone()["c"] == 0
    finally:
        conn.close()


def test_回放拒收失败闭批次背书的跨日半笔(临时库):
    日 = "2026-11-24"
    装 = 装载.装载流水([流水("g1", "户甲", 日, 1000)], "好批")
    抽取.日终抽取(日, "好抽")
    # 直接构造一笔由「失败闭装载批」背书的流水（服务路径下回滚本不会落库，
    # 此处验证回放守恒过滤器对历史半笔的拒收能力）
    conn = 连接()
    try:
        conn.execute(
            "INSERT INTO 装载批次(编号, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?)",
            ("坏批", "x", "{}", "租约-幽灵", "失败闭"))
        conn.execute(
            "INSERT INTO 变动流水(流水号, 账户键, 营业日, 发生时, 变动额, 摘要, 批次编号, 命运) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("坏1", "户甲", 日, f"{日}T10:00:00Z", 777, "半笔", "坏批", "已入账"))
        conn.commit()
    finally:
        conn.close()
    回 = 回放.回放核对(日, "回验")
    assert 回["半笔拒收"] >= 1
    assert 回["通过"] is False  # 拒收半笔后快照与回放不再守恒，落差额
    conn = 连接()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 差额行 WHERE 回放批次编号='回验'").fetchone()["c"] >= 1
    finally:
        conn.close()


def test_快照回滚持租约单事务打回半截镜(临时库):
    日 = "2026-11-25"
    装载.装载流水([流水("rb1", "户甲", 日, 600)], "回滚装")
    抽 = 抽取.日终抽取(日, "待回滚抽")
    conn = 连接()
    try:
        镜数 = conn.execute(
            "SELECT COUNT(*) AS c FROM 周期快照 WHERE 抽取批次编号='待回滚抽' AND 状态='有效'"
        ).fetchone()["c"]
        assert 镜数 >= 1
    finally:
        conn.close()
    r = 快照回滚.回滚快照("待回滚抽")
    assert r["状态"] == "已回滚" and r["回滚快照数"] == 镜数 and r["租约号"]
    conn = 连接()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 周期快照 WHERE 抽取批次编号='待回滚抽' AND 状态='有效'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT 状态 FROM 抽取批次 WHERE 编号='待回滚抽'").fetchone()["状态"] == "已回滚"
        # 回滚作业自身也在排队/租约实体留痕，同票
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 切日排队 WHERE 作业种类='快照回滚' AND 状态='完成'"
        ).fetchone()["c"] >= 1
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 四夹具端到端
# --------------------------------------------------------------------------
@pytest.mark.parametrize("名", [
    "切日并行双成功样例.json",
    "租约超时回滚样例.json",
    "多户半截回滚样例.json",
    "迟到绕锁排队样例.json",
])
def test_切日夹具全通过(临时库, 名):
    r = 切日夹具.运行切日夹具(名)
    assert r["全通过"] is True, [a for a in r["断言"] if not a["通过"]]
    assert r["双成功允许"] is True  # 乙口径夹具
