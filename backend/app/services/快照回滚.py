"""快照回滚（同票作业之一）。

回滚动作本身也必须先入切日 FIFO 取得租约（作业种类=快照回滚），
在单事务内把指定抽取批的快照打成「已回滚」、批次置「已回滚」后提交；
无有效租约拒绝执行，回滚与租约实体同票。
"""
from typing import Any

from app.db import 连接
from app.services import 切日租约


def 回滚快照(抽取批次编号: str, 持有方: str = "回滚调度",
             租约秒: float = 切日租约.默认租约秒,
             等待秒: float = 切日租约.默认等待秒) -> dict[str, Any]:
    if not 抽取批次编号.strip():
        raise ValueError("抽取批次编号为空")
    with 切日租约.切日上下文(持有方, "快照回滚", 租约秒, 等待秒) as 票:
        租约号 = 票["租约号"]
        conn = 连接()
        try:
            批 = conn.execute(
                "SELECT 编号 FROM 抽取批次 WHERE 编号=?", (抽取批次编号,)
            ).fetchone()
            if 批 is None:
                raise ValueError(f"抽取批次不存在: {抽取批次编号}")
            镜行 = conn.execute(
                "SELECT 快照键, 账户键, 营业日 FROM 周期快照 "
                "WHERE 抽取批次编号=? AND 状态='有效'",
                (抽取批次编号,),
            ).fetchall()
            for r in 镜行:
                conn.execute(
                    "UPDATE 周期快照 SET 状态='已回滚', 租约号=? WHERE 快照键=?",
                    (租约号, r["快照键"]),
                )
            conn.execute(
                "UPDATE 抽取批次 SET 状态='已回滚', 租约号=? WHERE 编号=?",
                (租约号, 抽取批次编号),
            )
            # 提交前最终围栏：失租则整笔回滚，不允许回滚动作本身留半截
            切日租约.核验(租约号, 持有方)
            conn.commit()
            return {
                "抽取批次编号": 抽取批次编号,
                "回滚快照数": len(镜行),
                "状态": "已回滚",
                "租约号": 租约号,
            }
        except 切日租约.租约已失:
            conn.rollback()
            raise
        finally:
            conn.close()
