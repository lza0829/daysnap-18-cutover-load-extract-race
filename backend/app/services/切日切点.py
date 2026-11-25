"""切日切点（口径乙：配对租约排队）。

切点动作与装载批、抽取批共用同一把切日租约、同一条 FIFO 队：
切点进行时，后到的装载/抽取只能排队或失败，不得无锁闯入。
"""
from typing import Any

from app.db import 连接
from app.services import 切日租约
from app.services.时间 import 标准化营业日


def 执行切点(至营业日: str, 持有方: str = "切点调度",
             租约秒: float = 切日租约.默认租约秒,
             等待秒: float = 切日租约.默认等待秒,
             持有时长秒: float = 0.0) -> dict[str, Any]:
    """把营业日切点推进到 至营业日。切点期间租约独占地持在切点动作上。

    持有时长秒>0 时在租约内保持窗口（夹具用于确定性制造「切日中后到作业排队」）。
    """
    至 = 标准化营业日(至营业日)
    try:
        with 切日租约.切日上下文(持有方, "切点", 租约秒, 等待秒) as 票:
            切日租约.核验(票["租约号"], 持有方)
            if 持有时长秒 > 0:
                import time
                限 = time.monotonic() + float(持有时长秒)
                while time.monotonic() < 限:
                    切日租约.核验(票["租约号"], 持有方)
                    time.sleep(min(0.02, max(0.0, 限 - time.monotonic())))
            conn = 连接()
            try:
                行 = conn.execute("SELECT 值 FROM 台参 WHERE 键='当前营业日'").fetchone()
                自营业日 = 行["值"] if 行 else "未初始化"
                conn.execute(
                    "INSERT INTO 台参(键, 值) VALUES ('当前营业日', ?) "
                    "ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                    (至,),
                )
                conn.commit()
            finally:
                conn.close()
            报告 = {
                "自营业日": 自营业日,
                "至营业日": 至,
                "租约号": 票["租约号"],
                "排队号": 票["排队号"],
            }
            批 = 切日租约.切点台账(自营业日, 至, 票["租约号"], "成功", 报告)
            return {"切日批次": 批, "状态": "成功", **报告}
    except 切日租约.排队超时 as e:
        return {"切日批次": None, "状态": "排队失败", **e.载荷()}
    except 切日租约.租约已失 as e:
        return {"切日批次": None, "状态": "失败闭", **e.载荷()}
