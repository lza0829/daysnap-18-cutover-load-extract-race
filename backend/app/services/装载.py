import json
import uuid
from typing import Any

from app.db import 连接
from app.services import 切日租约
from app.services.时间 import 此刻, 标准化营业日


def 装载流水(事件: list[dict[str, Any]], 批次编号: str = "",
             持有方: str = "装载调度",
             租约秒: float = 切日租约.默认租约秒,
             等待秒: float = 切日租约.默认等待秒) -> dict:
    """装载账户变动流水（金额单位：分）。

    口径乙：装载批先入切日 FIFO 取得租约后才允许写库；租约到期/被撤销 →
    整笔回滚（含已逐户写入的行），只落失败闭批次，不留半笔流水。
    """
    if not 事件:
        raise ValueError("流水列表为空")
    批 = 批次编号.strip() or f"装载-{uuid.uuid4().hex[:10]}"
    with 切日租约.切日上下文(持有方, "装载批", 租约秒, 等待秒) as 票:
        租约号 = 票["租约号"]
        conn = 连接()
        入账 = 0
        旁置 = 0
        try:
            for raw in 事件:
                # 每条写入前围栏核证：租约失效立即停止，未提交写整体回滚
                切日租约.故障点(租约号, 入账 + 旁置, 持有方)
                流水号 = str(raw.get("流水号") or "").strip() or f"流-{uuid.uuid4().hex[:12]}"
                账户键 = str(raw.get("账户键") or "").strip()
                if not 账户键:
                    conn.execute(
                        "INSERT INTO 旁置簿(原因, 账户键, 营业日, 载荷, 时刻) VALUES (?,?,?,?,?)",
                        ("缺账户键", "", "", json.dumps(raw, ensure_ascii=False), 此刻()),
                    )
                    旁置 += 1
                    continue
                户 = conn.execute("SELECT 账户键 FROM 账户 WHERE 账户键=?", (账户键,)).fetchone()
                if not 户:
                    conn.execute(
                        "INSERT INTO 旁置簿(原因, 账户键, 营业日, 载荷, 时刻) VALUES (?,?,?,?,?)",
                        ("未知账户", 账户键, "", json.dumps(raw, ensure_ascii=False), 此刻()),
                    )
                    旁置 += 1
                    continue
                try:
                    营业日 = 标准化营业日(str(raw.get("营业日") or ""))
                    变动额 = int(raw["变动额"])
                except Exception:
                    conn.execute(
                        "INSERT INTO 旁置簿(原因, 账户键, 营业日, 载荷, 时刻) VALUES (?,?,?,?,?)",
                        ("字段非法", 账户键, str(raw.get("营业日") or ""),
                         json.dumps(raw, ensure_ascii=False), 此刻()),
                    )
                    旁置 += 1
                    continue
                发生时 = str(raw.get("发生时") or f"{营业日}T12:00:00Z")
                摘要 = str(raw.get("摘要") or "")
                已有 = conn.execute("SELECT 流水号 FROM 变动流水 WHERE 流水号=?", (流水号,)).fetchone()
                if 已有:
                    conn.execute(
                        "INSERT INTO 旁置簿(原因, 账户键, 营业日, 载荷, 时刻) VALUES (?,?,?,?,?)",
                        ("流水号重复", 账户键, 营业日, json.dumps(raw, ensure_ascii=False), 此刻()),
                    )
                    旁置 += 1
                    continue
                conn.execute(
                    "INSERT INTO 变动流水(流水号, 账户键, 营业日, 发生时, 变动额, 摘要, 批次编号, 命运) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (流水号, 账户键, 营业日, 发生时, 变动额, 摘要, 批, "已入账"),
                )
                入账 += 1
            # 提交前最终围栏：失租则回滚全部未提交写，禁止半截流水落库
            切日租约.核验(租约号, 持有方)
            报告 = {"入账": 入账, "旁置": 旁置, "合计": len(事件), "租约号": 租约号}
            conn.execute(
                "INSERT INTO 装载批次(编号, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?)",
                (批, 此刻(), json.dumps(报告, ensure_ascii=False), 租约号, "成功"),
            )
            conn.commit()
            return {"批次编号": 批, **报告}
        except 切日租约.租约已失 as e:
            # 失败闭：回滚本连接全部未提交写（含多户已写行），仅留失败台账
            conn.rollback()
            try:
                报告 = {"入账": 0, "旁置": 旁置, "合计": len(事件), "租约号": 租约号,
                        "失败闭": True, "原因": str(e)}
                conn.execute(
                    "INSERT INTO 装载批次(编号, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?)",
                    (批, 此刻(), json.dumps(报告, ensure_ascii=False), 租约号, "失败闭"),
                )
                conn.commit()
            finally:
                pass
            raise
        finally:
            conn.close()
