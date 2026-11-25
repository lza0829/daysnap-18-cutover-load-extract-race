"""切日互斥核（配对租约排队 · 口径乙）。

口径（全局唯一）：
- 切日窗口内装载批、抽取批、切点动作、回放核对、快照回滚一律先入「切日排队」FIFO，
  队头且无有效租约时才能取得「切日租约」实体；后到方排队等待，两侧最终都可有序成功。
- 互斥凭据是库内实体（切日租约/切日排队），跨进程/跨连接可见；禁止进程内私有锁冒充。
- 租约到期：valid 置 0、排队行置「失败闭」，持有侧未提交写必须回滚，不得留半截镜/半笔流水。
- 无有效租约的写路径一律拒绝（迟到补抽不得绕锁写入）。
"""
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any

from app.db import 连接
from app.services.时间 import 单调秒, 此刻

口径 = "配对租约排队"
双成功允许 = True  # 乙：并行各发起一次最终可双成功，但须守恒；甲口径此常量应为 False

默认租约秒 = 30.0
默认等待秒 = 60.0
轮询间隔秒 = 0.05

# 故障注入 seam：签名 (租约号, 进度)。装载/抽取每处理一行/一户回调一次，
# 供夹具与自动化测试确定性制造「持租约中途丢失」（如回调内 撤销租约）。生产恒为 None。
故障钩子 = None

作业种类 = ("装载批", "抽取批", "切点", "回放批", "快照回滚")


class 租约异常(RuntimeError):
    """切日租约相关失败的基类。"""

    def 载荷(self) -> dict[str, Any]:
        return {"原因": str(self)}


class 排队超时(租约异常):
    """等待租约超过预算，未取得任何写权。"""


class 租约已失(租约异常):
    """租约到期或被撤销，未提交写必须失败闭并回滚。"""


class 无租约写入(租约异常):
    """未持有效租约即试图写业务表（迟到补抽绕锁等）。"""


def _新令牌() -> str:
    return f"租约-{uuid.uuid4().hex[:12]}"


def _清理到期租约(conn) -> list[Any]:
    """把已过期但仍 valid 的租约打成失败闭（reaper 由每次申请动作顺带驱动）。"""
    now = 单调秒()
    行们 = conn.execute(
        "SELECT * FROM 切日租约 WHERE valid=1 AND 租约截止<=?", (now,)
    ).fetchall()
    for r in 行们:
        conn.execute("UPDATE 切日租约 SET valid=0 WHERE 租约号=?", (r["租约号"],))
        conn.execute(
            "UPDATE 切日排队 SET 状态='失败闭', 备注=? WHERE 排队号=? AND 状态 IN ('等待','持有')",
            ("租约到期失败闭", r["排队号"]),
        )
    return 行们


def 核证(租约号: str | None = None) -> dict[str, Any]:
    """切日互斥核证接口：持有方、租约截止、valid。

    无参返回当前有效租约（至多一行）；带参核证指定租约并校验持有状态。
    """
    conn = 连接()
    try:
        _清理到期租约(conn)
        conn.commit()
        if 租约号:
            r = conn.execute("SELECT * FROM 切日租约 WHERE 租约号=?", (租约号,)).fetchone()
            if r is None:
                return {"租约号": 租约号, "持有方": None, "租约截止": None, "valid": False,
                        "原因": "租约不存在"}
            valid = bool(r["valid"]) and float(r["租约截止"]) > 单调秒()
            return {
                "租约号": r["租约号"],
                "持有方": r["持有方"],
                "作业种类": r["作业种类"],
                "取得时刻": r["取得时刻"],
                "租约截止": r["租约截止"],
                "valid": valid,
            }
        r = conn.execute(
            "SELECT * FROM 切日租约 WHERE valid=1 AND 租约截止>? ORDER BY 取得时刻 DESC LIMIT 1",
            (单调秒(),),
        ).fetchone()
        等待数 = conn.execute("SELECT COUNT(*) AS c FROM 切日排队 WHERE 状态='等待'").fetchone()["c"]
        if r is None:
            return {"租约号": None, "持有方": None, "租约截止": None, "valid": False,
                    "排队等待数": 等待数, "口径": 口径}
        return {
            "租约号": r["租约号"],
            "持有方": r["持有方"],
            "作业种类": r["作业种类"],
            "取得时刻": r["取得时刻"],
            "租约截止": r["租约截止"],
            "valid": True,
            "排队等待数": 等待数,
            "口径": 口径,
        }
    finally:
        conn.close()


def 排队视图() -> list[dict[str, Any]]:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 切日排队 ORDER BY 排队号 DESC LIMIT 50"
        )]
    finally:
        conn.close()


def 撤销租约(租约号: str, 备注: str = "人工撤销") -> bool:
    """管理/故障注入：把租约打成失败闭。持有侧下次核证即抛 租约已失，其未提交写回滚。"""
    conn = 连接()
    try:
        r = conn.execute("SELECT * FROM 切日租约 WHERE 租约号=? AND valid=1", (租约号,)).fetchone()
        if r is None:
            return False
        conn.execute("UPDATE 切日租约 SET valid=0 WHERE 租约号=?", (租约号,))
        conn.execute(
            "UPDATE 切日排队 SET 状态='失败闭', 备注=? WHERE 排队号=? AND 状态 IN ('等待','持有')",
            (备注, r["排队号"]),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def 续租(租约号: str, 增量秒: float) -> None:
    """持有方在作业中途延长截止；非持有者/已失租约续租即报错。"""
    if 增量秒 <= 0:
        raise 租约异常("续租增量须为正")
    conn = 连接()
    try:
        r = conn.execute("SELECT * FROM 切日租约 WHERE 租约号=?", (租约号,)).fetchone()
        if r is None or not r["valid"] or float(r["租约截止"]) <= 单调秒():
            raise 租约已失("租约已失，拒绝续租")
        conn.execute(
            "UPDATE 切日租约 SET 租约截止=? WHERE 租约号=?",
            (float(r["租约截止"]) + float(增量秒), 租约号),
        )
        conn.commit()
    finally:
        conn.close()


def 申请租约(持有方: str, 作业种类: str, 租约秒: float = 默认租约秒,
             等待秒: float = 默认等待秒) -> dict[str, Any]:
    """入 FIFO 队并阻塞至取得租约。返回租约描述；超时抛 排队超时（不留任何业务写）。"""
    if 作业种类 not in 作业种类:
        raise 租约异常(f"未知作业种类: {作业种类}")
    if 租约秒 <= 0:
        raise 租约异常("租约时长须为正")
    截止等待 = 单调秒() + max(0.0, 等待秒)
    conn = 连接()
    try:
        _清理到期租约(conn)
        cur = conn.execute(
            "INSERT INTO 切日排队(持有方, 作业种类, 入队时刻, 状态) VALUES (?,?,?,?)",
            (持有方, 作业种类, 单调秒(), "等待"),
        )
        排队号 = cur.lastrowid
        conn.commit()  # 排队落库即对其他进程可见
    finally:
        conn.close()

    while True:
        conn = 连接()
        try:
            _清理到期租约(conn)
            队头 = conn.execute(
                "SELECT * FROM 切日排队 WHERE 状态 IN ('等待','持有') ORDER BY 排队号 ASC LIMIT 1"
            ).fetchone()
            if 队头 is not None and 队头["排队号"] == 排队号:
                有效租约 = conn.execute(
                    "SELECT * FROM 切日租约 WHERE valid=1 AND 租约截止>?",
                    (单调秒(),),
                ).fetchone()
                if 有效租约 is None:
                    租约号 = _新令牌()
                    now = 单调秒()
                    try:
                        conn.execute(
                            "INSERT INTO 切日租约(租约号, 持有方, 作业种类, 取得时刻, 租约截止, 排队号, valid) "
                            "VALUES (?,?,?,?,?,?,1)",
                            (租约号, 持有方, 作业种类, now, now + float(租约秒), 排队号),
                        )
                    except sqlite3.IntegrityError:
                        # 库级唯一索引兜底：并发下他方已先建有效租约，本连接回滚后继续排队轮询
                        conn.rollback()
                        continue
                    conn.execute(
                        "UPDATE 切日排队 SET 状态='持有', 租约号=? WHERE 排队号=?",
                        (租约号, 排队号),
                    )
                    conn.commit()
                    return {
                        "租约号": 租约号,
                        "排队号": 排队号,
                        "持有方": 持有方,
                        "作业种类": 作业种类,
                        "租约截止": now + float(租约秒),
                    }
            自己 = conn.execute("SELECT * FROM 切日排队 WHERE 排队号=?", (排队号,)).fetchone()
            if 自己 is not None and 自己["状态"] == "失败闭":
                conn.commit()
                raise 租约已失(f"排队号 {排队号} 已失败闭：{自己['备注']}")
            conn.commit()
        finally:
            conn.close()
        if 单调秒() > 截止等待:
            conn = 连接()
            try:
                conn.execute(
                    "UPDATE 切日排队 SET 状态='失败闭', 备注='排队等待超时' WHERE 排队号=? AND 状态='等待'",
                    (排队号,),
                )
                conn.commit()
            finally:
                conn.close()
            raise 排队超时(f"{持有方} 等待切日租约超过 {等待秒}s，未取得写权")
        import time
        time.sleep(轮询间隔秒)


def 释放租约(租约号: str) -> None:
    """正常完成：valid=0 且排队行置完成，FIFO 下一位方可取得。"""
    conn = 连接()
    try:
        r = conn.execute("SELECT * FROM 切日租约 WHERE 租约号=?", (租约号,)).fetchone()
        if r is None:
            return
        conn.execute("UPDATE 切日租约 SET valid=0 WHERE 租约号=?", (租约号,))
        conn.execute(
            "UPDATE 切日排队 SET 状态='完成' WHERE 排队号=? AND 状态='持有'",
            (r["排队号"],),
        )
        conn.commit()
    finally:
        conn.close()


def 失租约闭(租约号: str, 备注: str = "租约到期失败闭") -> None:
    """失败闭：valid=0 且排队行置失败闭（与回滚配对调用）。"""
    conn = 连接()
    try:
        r = conn.execute("SELECT * FROM 切日租约 WHERE 租约号=?", (租约号,)).fetchone()
        if r is None:
            return
        conn.execute("UPDATE 切日租约 SET valid=0 WHERE 租约号=?", (租约号,))
        conn.execute(
            "UPDATE 切日排队 SET 状态='失败闭', 备注=? WHERE 排队号=? AND 状态 IN ('等待','持有')",
            (备注, r["排队号"],),
        )
        conn.commit()
    finally:
        conn.close()


def 核验(租约号: str, 持有方: str | None = None) -> dict[str, Any]:
    """写事务提交前的围栏校验：租约必须仍 valid 且未到期，否则抛 租约已失。"""
    conn = 连接()
    try:
        r = conn.execute("SELECT * FROM 切日租约 WHERE 租约号=?", (租约号,)).fetchone()
        if r is None or not r["valid"] or float(r["租约截止"]) <= 单调秒():
            raise 租约已失(f"租约 {租约号} 已失效（到期/撤销），未提交写须回滚")
        if 持有方 is not None and r["持有方"] != 持有方:
            raise 租约已失(f"租约持有方不符：期望 {持有方}，实际 {r['持有方']}")
        return dict(r)
    finally:
        conn.close()


def 故障点(租约号: str, 进度: int, 持有方: str | None = None) -> dict[str, Any]:
    """每行/每户围栏：先跑故障注入钩子（可在此撤销租约），再核证 valid。"""
    if 故障钩子 is not None:
        故障钩子(租约号, 进度)
    return 核验(租约号, 持有方)


def 确保持租约(租约号: str | None) -> str:
    """无有效租约即拒绝写入（迟到补抽绕锁闸门）。"""
    if not 租约号:
        raise 无租约写入("未持切日租约，禁止写入（迟到补抽须重新排队取租约）")
    核验(租约号)
    return 租约号


@contextmanager
def 切日上下文(持有方: str, 作业种类: str, 租约秒: float = 默认租约秒,
                等待秒: float = 默认等待秒):
    """取得租约 → 交出租约描述；正常退出释放，异常退出按到期/失败闭处理。

    业务写事务须在本上下文内开启，并在 commit 前调用 核验()；
    上下文只负责租约实体生命周期，业务回滚由调用方在 except 中 conn.rollback()。
    """
    票 = 申请租约(持有方, 作业种类, 租约秒, 等待秒)
    try:
        yield 票
    except 租约已失:
        失租约闭(票["租约号"], "持有侧租约失效，失败闭")
        raise
    except Exception:
        # 业务异常：租约本身仍有效，正常释放给下一位，业务回滚由调用方负责
        释放租约(票["租约号"])
        raise
    else:
        释放租约(票["租约号"])


def 切点台账(自营业日: str, 至营业日: str, 租约号: str, 状态: str, 报告: dict[str, Any]) -> str:
    import json
    批 = f"切日-{至营业日}-{uuid.uuid4().hex[:8]}"
    conn = 连接()
    try:
        conn.execute(
            "INSERT INTO 切日批次(编号, 自营业日, 至营业日, 时刻, 租约号, 状态, 报告) "
            "VALUES (?,?,?,?,?,?,?)",
            (批, 自营业日, 至营业日, 此刻(), 租约号, 状态,
             json.dumps(报告, ensure_ascii=False)),
        )
        conn.commit()
        return 批
    finally:
        conn.close()
