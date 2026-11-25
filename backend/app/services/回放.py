import json
import uuid
from typing import Any

from app.db import 连接
from app.services import 切日租约
from app.services.时间 import 此刻, 标准化营业日


def _前日(营业日: str) -> str:
    y, m, d = map(int, 营业日.split("-"))
    from datetime import date, timedelta

    return (date(y, m, d) - timedelta(days=1)).isoformat()


def 回放核对(营业日: str, 批次编号: str = "",
             持有方: str = "回放调度",
             租约秒: float = 切日租约.默认租约秒,
             等待秒: float = 切日租约.默认等待秒) -> dict[str, Any]:
    """用前日快照+当日流水重算，对照当日周期快照，写入差额/跳变。

    口径乙协同：回放批也入切日 FIFO 持租约，保证不会读到切日窗口内的半截写；
    回放只接受完整批背书的流水（同票装载批非失败闭），跨日半笔一律不接受。
    """
    日 = 标准化营业日(营业日)
    批 = 批次编号.strip() or f"回放-{日}-{uuid.uuid4().hex[:8]}"
    with 切日租约.切日上下文(持有方, "回放批", 租约秒, 等待秒) as 票:
        租约号 = 票["租约号"]
        conn = 连接()
        try:
            参 = {r["键"]: r["值"] for r in conn.execute("SELECT 键, 值 FROM 台参")}
            try:
                阈值 = int(参.get("跳变阈值分", "0") or "0")
            except ValueError:
                阈值 = 0
            账户们 = [dict(r) for r in conn.execute("SELECT * FROM 账户 WHERE 状态='正常'")]
            相符 = 0
            差额数 = 0
            跳变数 = 0
            无快照 = 0
            半笔拒 = 0
            # 失败闭装载批的流水号即「跨日半笔」候选；回放不接受其参与守恒
            失败批 = {
                r["编号"] for r in conn.execute(
                    "SELECT 编号 FROM 装载批次 WHERE 状态='失败闭'"
                )
            }
            for 户 in 账户们:
                切日租约.核验(租约号, 持有方)
                账户键 = 户["账户键"]
                if 日 < 户["开户营业日"]:
                    continue
                快照 = conn.execute(
                    "SELECT 余额 FROM 周期快照 WHERE 账户键=? AND 营业日=? AND 状态='有效'",
                    (账户键, 日),
                ).fetchone()
                if not 快照:
                    无快照 += 1
                    conn.execute(
                        "INSERT INTO 差额行(回放批次编号, 账户键, 营业日, 快照余额, 回放余额, 差额, 原因, 时刻) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (批, 账户键, 日, None, None, 0, "缺当日快照（拒读半截镜）", 此刻()),
                    )
                    差额数 += 1
                    continue
                前 = _前日(日)
                前日快照 = conn.execute(
                    "SELECT 余额 FROM 周期快照 WHERE 账户键=? AND 营业日=? AND 状态='有效'",
                    (账户键, 前),
                ).fetchone()
                开端 = 0 if 前日快照 is None else int(前日快照["余额"])
                # 只接受非失败闭批次背书的入账流水，杜绝跨日半笔混入守恒
                行们 = conn.execute(
                    "SELECT 变动额, 批次编号 FROM 变动流水 "
                    "WHERE 账户键=? AND 营业日=? AND 命运='已入账'",
                    (账户键, 日),
                ).fetchall()
                净额 = 0
                for r in 行们:
                    if r["批次编号"] in 失败批:
                        半笔拒 += 1
                        continue
                    净额 += int(r["变动额"])
                回放余额 = 开端 + 净额
                快照余额 = int(快照["余额"])
                差额 = 快照余额 - 回放余额
                if 差额 == 0 and 半笔拒 == 0:
                    相符 += 1
                else:
                    conn.execute(
                        "INSERT INTO 差额行(回放批次编号, 账户键, 营业日, 快照余额, 回放余额, 差额, 原因, 时刻) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (批, 账户键, 日, 快照余额, 回放余额, 差额,
                         "快照与回放不一致（含跨日半笔拒收）" if 半笔拒 else "快照与回放不一致",
                         此刻()),
                    )
                    差额数 += 1
                偏离 = abs(快照余额 - (开端 + 净额))
                if 阈值 > 0 and 偏离 >= 阈值:
                    conn.execute(
                        "INSERT INTO 跳变记录(账户键, 营业日, 前日余额, 当日余额, 流水净额, 偏离, 检出批次编号, 时刻) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (账户键, 日, 开端, 快照余额, 净额, 偏离, 批, 此刻()),
                    )
                    跳变数 += 1
            通过 = 差额数 == 0 and 无快照 == 0 and 半笔拒 == 0
            报告 = {
                "营业日": 日,
                "相符": 相符,
                "差额": 差额数,
                "跳变": 跳变数,
                "无快照": 无快照,
                "半笔拒收": 半笔拒,
                "通过": 通过,
                "租约号": 租约号,
            }
            conn.execute(
                "INSERT INTO 回放批次(编号, 营业日, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?,?)",
                (批, 日, 此刻(), json.dumps(报告, ensure_ascii=False), 租约号, "成功"),
            )
            conn.commit()
            return {"批次编号": 批, **报告}
        except 切日租约.租约已失 as e:
            conn.rollback()
            try:
                报告 = {"营业日": 日, "相符": 0, "差额": 0, "跳变": 0, "无快照": 0,
                        "半笔拒收": 0, "通过": False, "租约号": 租约号,
                        "失败闭": True, "原因": str(e)}
                conn.execute(
                    "INSERT INTO 回放批次(编号, 营业日, 时刻, 报告, 租约号, 状态) VALUES (?,?,?,?,?,?)",
                    (批, 日, 此刻(), json.dumps(报告, ensure_ascii=False), 租约号, "失败闭"),
                )
                conn.commit()
            finally:
                pass
            raise
        finally:
            conn.close()
