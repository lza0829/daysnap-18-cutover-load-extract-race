import json
import uuid
from typing import Any

from app.db import 连接
from app.services import 切日租约
from app.services.时间 import 此刻, 标准化营业日


def _台参(conn) -> dict[str, str]:
    return {r["键"]: r["值"] for r in conn.execute("SELECT 键, 值 FROM 台参")}


def _前日(营业日: str) -> str:
    y, m, d = map(int, 营业日.split("-"))
    from datetime import date, timedelta

    return (date(y, m, d) - timedelta(days=1)).isoformat()


def 日终抽取(营业日: str, 批次编号: str = "",
             持有方: str = "抽取调度",
             租约秒: float = 切日租约.默认租约秒,
             等待秒: float = 切日租约.默认等待秒) -> dict[str, Any]:
    """按营业日为全部正常账户写周期快照。余额=前日快照(或开户零)+当日流水净额。

    口径乙：抽取批先入切日 FIFO 取租约；租约到期/被撤销 → 快照写整体回滚，
    只落失败闭批次，禁止半截日镜。迟到补抽同样必须重新排队取租约，不得绕锁写入。
    """
    日 = 标准化营业日(营业日)
    批 = 批次编号.strip() or f"抽取-{日}-{uuid.uuid4().hex[:8]}"
    with 切日租约.切日上下文(持有方, "抽取批", 租约秒, 等待秒) as 票:
        租约号 = 票["租约号"]
        conn = 连接()
        try:
            参 = _台参(conn)
            重复 = 参.get("重复抽取", "盖写同日快照")
            缺前日 = 参.get("缺前日策略", "以零为开端")
            账户们 = [
                dict(r)
                for r in conn.execute("SELECT * FROM 账户 WHERE 状态='正常' ORDER BY 账户键")
            ]
            写入 = 0
            盖写 = 0
            旁置 = 0
            断档数 = 0
            已处理 = 0
            for 户 in 账户们:
                # 逐户围栏：租约失效即停，未提交快照整体回滚，杜绝半截镜
                切日租约.故障点(租约号, 已处理, 持有方)
                已处理 += 1
                账户键 = 户["账户键"]
                开户日 = 户["开户营业日"]
                if 日 < 开户日:
                    continue
                前 = _前日(日)
                前日快照 = conn.execute(
                    "SELECT 余额 FROM 周期快照 WHERE 账户键=? AND 营业日=? AND 状态='有效'",
                    (账户键, 前),
                ).fetchone()
                if 前日快照 is None:
                    if 日 == 开户日 or 缺前日 == "以零为开端":
                        开端 = 0
                    else:
                        conn.execute(
                            "INSERT INTO 断档记录(账户键, 缺营业日, 检出批次编号, 处置, 时刻) "
                            "VALUES (?,?,?,?,?)",
                            (账户键, 前, 批, "记断档并继续", 此刻()),
                        )
                        断档数 += 1
                        开端 = 0
                else:
                    开端 = int(前日快照["余额"])
                净额行 = conn.execute(
                    "SELECT COALESCE(SUM(变动额),0) AS s FROM 变动流水 "
                    "WHERE 账户键=? AND 营业日=? AND 命运='已入账'",
                    (账户键, 日),
                ).fetchone()
                净额 = int(净额行["s"])
                余额 = 开端 + 净额
                快照键 = f"{账户键}|{日}"
                旧 = conn.execute(
                    "SELECT 快照键 FROM 周期快照 WHERE 账户键=? AND 营业日=?",
                    (账户键, 日),
                ).fetchone()
                if 旧:
                    if 重复 != "盖写同日快照":
                        conn.execute(
                            "INSERT INTO 旁置簿(原因, 账户键, 营业日, 载荷, 时刻) VALUES (?,?,?,?,?)",
                            (
                                "同日已有快照",
                                账户键,
                                日,
                                json.dumps({"余额": 余额}, ensure_ascii=False),
                                此刻(),
                            ),
                        )
                        旁置 += 1
                        continue
                    conn.execute(
                        "UPDATE 周期快照 SET 余额=?, 抽取批次编号=?, 来源='日终抽取', 状态='有效', 租约号=? "
                        "WHERE 账户键=? AND 营业日=?",
                        (余额, 批, 租约号, 账户键, 日),
                    )
                    盖写 += 1
                else:
                    conn.execute(
                        "INSERT INTO 周期快照(快照键, 账户键, 营业日, 余额, 抽取批次编号, 来源, 状态, 租约号) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (快照键, 账户键, 日, 余额, 批, "日终抽取", "有效", 租约号),
                    )
                    写入 += 1
            # 提交前最终围栏
            切日租约.核验(租约号, 持有方)
            报告 = {
                "营业日": 日,
                "写入": 写入,
                "盖写": 盖写,
                "旁置": 旁置,
                "断档": 断档数,
                "账户扫描": len(账户们),
                "租约号": 租约号,
            }
            conn.execute(
                "INSERT INTO 抽取批次(编号, 营业日, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?,?)",
                (批, 日, 此刻(), json.dumps(报告, ensure_ascii=False), 租约号, "成功"),
            )
            conn.commit()
            return {"批次编号": 批, **报告}
        except 切日租约.租约已失 as e:
            # 失败闭：回滚本批全部快照写，禁止半截日镜
            conn.rollback()
            try:
                报告 = {"营业日": 日, "写入": 0, "盖写": 0, "旁置": 0, "断档": 0,
                        "账户扫描": 0, "租约号": 租约号, "失败闭": True, "原因": str(e)}
                conn.execute(
                    "INSERT INTO 抽取批次(编号, 营业日, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?,?)",
                    (批, 日, 此刻(), json.dumps(报告, ensure_ascii=False), 租约号, "失败闭"),
                )
                conn.commit()
            finally:
                pass
            raise
        finally:
            conn.close()
