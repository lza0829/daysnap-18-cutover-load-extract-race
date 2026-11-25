from app.db import 连接
from app.services import 切日租约


def 总览() -> dict:
    conn = 连接()
    try:
        def 数(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        out = {
            "账户数": 数("SELECT COUNT(*) FROM 账户"),
            "流水数": 数("SELECT COUNT(*) FROM 变动流水 WHERE 命运='已入账'"),
            "快照数": 数("SELECT COUNT(*) FROM 周期快照 WHERE 状态='有效'"),
            "差额行数": 数("SELECT COUNT(*) FROM 差额行"),
            "断档数": 数("SELECT COUNT(*) FROM 断档记录"),
            "跳变数": 数("SELECT COUNT(*) FROM 跳变记录"),
            "旁置数": 数("SELECT COUNT(*) FROM 旁置簿"),
            "抽取批次数": 数("SELECT COUNT(*) FROM 抽取批次"),
            "回放批次数": 数("SELECT COUNT(*) FROM 回放批次"),
        }
        out["切日口径"] = 切日租约.口径
        out["双成功允许"] = 切日租约.双成功允许
        out["失败闭批次数"] = 数(
            "SELECT (SELECT COUNT(*) FROM 装载批次 WHERE 状态='失败闭')"
            " + (SELECT COUNT(*) FROM 抽取批次 WHERE 状态='失败闭')"
            " + (SELECT COUNT(*) FROM 回放批次 WHERE 状态='失败闭')"
        )
        return out
    finally:
        conn.close()
