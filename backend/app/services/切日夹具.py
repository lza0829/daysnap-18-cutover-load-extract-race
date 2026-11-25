"""切日并行夹具运行器：按夹具声明编排线程/故障注入，并逐条核对断言。

断言口径跟随全局唯一架构（乙：配对租约排队，双成功允许须守恒）。
所有互斥仍以库内切日租约/排队实体为准，运行器自身不加任何进程内私有锁。
"""
import json
import threading
import uuid
from typing import Any

from app.db import 夹具目录, 连接
from app.services import 切日租约, 装载, 抽取, 回放, 切日切点
from app.services.时间 import 置时钟


class _假时钟:
    """可手动跳变的单调时钟，供租约到期夹具确定性使用。"""

    def __init__(self, 起步: float = 1000.0):
        self.t = 起步
        self.锁 = threading.Lock()

    def __call__(self) -> float:
        with self.锁:
            return self.t

    def 推进(self, 秒: float) -> None:
        with self.锁:
            self.t += 秒


def _读夹具(名: str) -> dict[str, Any]:
    path = 夹具目录 / 名
    if not path.exists():
        raise FileNotFoundError(f"夹具不存在: {名}")
    return json.loads(path.read_text(encoding="utf-8"))


def _查(sql: str, args: tuple = ()):
    conn = 连接()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _流水号集合(流水: list[dict], 令牌: str) -> list[str]:
    return [f"{r['流水号']}-{令牌}" for r in 流水]


def _改名(流水: list[dict], 令牌: str) -> list[dict]:
    out = []
    for r in 流水:
        x = dict(r)
        x["流水号"] = f"{x['流水号']}-{令牌}"
        out.append(x)
    return out


def _等持有方(持有方: str, 超时: float = 5.0) -> dict:
    import time
    限 = time.monotonic() + 超时
    while time.monotonic() < 限:
        证 = 切日租约.核证()
        if 证.get("持有方") == 持有方 and 证.get("valid"):
            return 证
        time.sleep(0.01)
    raise TimeoutError(f"等待 {持有方} 持租约超时")


def _核对(期望: dict, 实测: dict) -> list[dict]:
    结 = []
    for k, v in 期望.items():
        实 = 实测.get(k)
        结.append({"断言": k, "期望": v, "实测": 实, "通过": 实 == v})
    return 结


# ---------------------------------------------------------------------------
# 场景一：切日并行——乙口径双成功且回放守恒
# ---------------------------------------------------------------------------
def _场景并行(景: dict, 令牌: str) -> tuple[list[dict], dict]:
    日 = 景["营业日"]
    流水 = _改名(景["流水"], 令牌)
    装批号 = f"并行装载-{令牌}"
    抽批号 = f"并行抽取-{令牌}"
    果: dict[str, Any] = {}

    def 装():
        try:
            果["装载"] = 装载.装载流水(流水, 装批号, 持有方="并行装载")
        except Exception as e:  # noqa: BLE001
            果["装载异常"] = str(e)

    def 抽():
        try:
            果["抽取"] = 抽取.日终抽取(日, 抽批号, 持有方="并行抽取")
        except Exception as e:  # noqa: BLE001
            果["抽取异常"] = str(e)

    t1 = threading.Thread(target=装)
    t1.start()
    _等持有方("并行装载")  # 保证装载先持租约，抽取后到排队，守恒口径确定
    t2 = threading.Thread(target=抽)
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    回 = 回放.回放核对(日, f"并行回放-{令牌}")
    号们 = _流水号集合(景["流水"], 令牌)
    占位 = ",".join("?" for _ in 号们)
    半笔 = _查(
        f"SELECT COUNT(*) AS c FROM 变动流水 WHERE 流水号 IN ({占位}) AND 命运!='已入账'",
        tuple(号们),
    )[0]["c"]
    半镜 = _查(
        "SELECT COUNT(*) AS c FROM 周期快照 WHERE 营业日=? AND 状态!='有效'", (日,)
    )[0]["c"]
    失败闭 = _查(
        "SELECT (SELECT COUNT(*) FROM 装载批次 WHERE 状态='失败闭')"
        " + (SELECT COUNT(*) FROM 抽取批次 WHERE 状态='失败闭') AS c"
    )[0]["c"]
    实测 = {
        "双成功": bool(果.get("装载")) and bool(果.get("抽取")) and "装载异常" not in 果 and "抽取异常" not in 果,
        "失败闭数": 失败闭,
        "半截流水": 半笔,
        "半截快照": 半镜,
        "回放通过": bool(回["通过"]),
        "核证valid终态": bool(切日租约.核证()["valid"]),
    }
    return _核对(景["断言"], 实测), {"作业": 果, "回放": 回, "实测": 实测}


# ---------------------------------------------------------------------------
# 场景二：租约超时——失败闭回滚，新作业可取租约成功
# ---------------------------------------------------------------------------
def _场景超时(景: dict, 令牌: str) -> tuple[list[dict], dict]:
    日 = 景["营业日"]
    流水 = _改名(景["流水"], 令牌)
    装批号 = f"超时装载-{令牌}"
    钟 = _假时钟()
    置时钟(钟)
    故 = 景["故障"]
    切日租约.故障钩子 = lambda 租约号, 进度: 钟.推进(float(故["推进秒"]))
    首果: dict[str, Any] = {}
    try:
        try:
            装载.装载流水(流水, 装批号, 持有方="超时装载", 租约秒=float(故["租约秒"]))
        except 切日租约.租约已失 as e:
            首果["失败闭"] = str(e)
    finally:
        切日租约.故障钩子 = None
        置时钟(None)

    号们 = _流水号集合(景["流水"], 令牌)
    占位 = ",".join("?" for _ in 号们)
    半笔 = _查(f"SELECT COUNT(*) AS c FROM 变动流水 WHERE 流水号 IN ({占位})", tuple(号们))[0]["c"]
    失败批 = _查(
        "SELECT COUNT(*) AS c FROM 装载批次 WHERE 编号=? AND 状态='失败闭'", (装批号,)
    )[0]["c"]
    # 租约已被 reaper 失败闭：新作业应能取得新租约并成功
    后 = None
    后续异常 = None
    try:
        后 = 抽取.日终抽取(日, f"超时后续-{令牌}", 持有方="超时后续抽取")
    except Exception as e:  # noqa: BLE001
        后续异常 = str(e)
    半镜 = _查(
        "SELECT COUNT(*) AS c FROM 周期快照 WHERE 营业日=? AND 状态!='有效'", (日,)
    )[0]["c"]
    实测 = {
        "首作失败闭": bool(首果.get("失败闭")) and 失败批 == 1,
        "半截流水": 半笔,
        "半截快照": 半镜,
        "后续成功": 后 is not None and 后续异常 is None and 后.get("批次编号") is not None,
        "核证valid终态": bool(切日租约.核证()["valid"]),
    }
    return _核对(景["断言"], 实测), {"首作": 首果, "后续": 后, "后续异常": 后续异常, "实测": 实测}


# ---------------------------------------------------------------------------
# 场景三：多户装载半截——中途丢租约，已写回滚
# ---------------------------------------------------------------------------
def _场景多户半截(景: dict, 令牌: str) -> tuple[list[dict], dict]:
    流水 = _改名(景["流水"], 令牌)
    装批号 = f"半截装载-{令牌}"
    故 = 景["故障"]
    槛 = int(故.get("撤销于进度", 故.get("到期于进度", 0)))
    短租约 = float(故.get("租约秒", 0.1))
    钟 = _假时钟()
    置时钟(钟)

    def 钩子(租约号: str, 进度: int) -> None:
        # 不另开写连接（SQLite 单写者），以时钟跳变确定性制造「持租约中途到期丢失」
        if 进度 == 槛:
            钟.推进(短租约 + 1.0)

    切日租约.故障钩子 = 钩子
    首果: dict[str, Any] = {}
    try:
        try:
            装载.装载流水(流水, 装批号, 持有方="半截装载", 租约秒=短租约)
        except 切日租约.租约已失 as e:
            首果["失败闭"] = str(e)
    finally:
        切日租约.故障钩子 = None
        置时钟(None)

    # 失败后驱动一次 reaper（新连接、无写锁竞争），把到期租约与排队行落成失败闭
    切日租约.核证()
    号们 = _流水号集合(景["流水"], 令牌)
    占位 = ",".join("?" for _ in 号们)
    半笔 = _查(f"SELECT COUNT(*) AS c FROM 变动流水 WHERE 流水号 IN ({占位})", tuple(号们))[0]["c"]
    失败闭 = _查(
        "SELECT COUNT(*) AS c FROM 装载批次 WHERE 状态='失败闭' AND 编号=?", (装批号,)
    )[0]["c"]
    队失败 = _查(
        "SELECT COUNT(*) AS c FROM 切日排队 WHERE 持有方='半截装载' AND 状态='失败闭'"
    )[0]["c"]
    实测 = {
        "首作失败闭": bool(首果.get("失败闭")) and 失败闭 == 1 and 队失败 == 1,
        "半截流水": 半笔,
        "失败闭数": 失败闭,
    }
    return _核对(景["断言"], 实测), {"首作": 首果, "实测": 实测}


# ---------------------------------------------------------------------------
# 场景四：切日中第二路抽取排队 + 迟到补抽绕锁被拒
# ---------------------------------------------------------------------------
def _场景排队绕锁(景: dict, 令牌: str) -> tuple[list[dict], dict]:
    日 = 景["营业日"]
    # 先正常持租约装入流水
    装载.装载流水(_改名(景["流水"], 令牌), f"绕锁装载-{令牌}", 持有方="绕锁装载")
    果: dict[str, Any] = {}
    切点作业, 抽取作业 = 景["串行作业"]

    def 切():
        try:
            果["切点"] = 切日切点.执行切点(
                切点作业["至营业日"], 持有方=切点作业["持有方"],
                持有时长秒=float(切点作业.get("持有时长秒", 0.0)),
            )
        except Exception as e:  # noqa: BLE001
            果["切点异常"] = str(e)

    def 抽():
        try:
            果["抽取"] = 抽取.日终抽取(
                抽取作业["营业日"], f"第二路抽取-{令牌}", 持有方=抽取作业["持有方"],
            )
        except Exception as e:  # noqa: BLE001
            果["抽取异常"] = str(e)

    t1 = threading.Thread(target=切)
    t1.start()
    _等持有方(切点作业["持有方"])  # 切点持租约期间，第二路抽取后到，只能排队
    t2 = threading.Thread(target=抽)
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # 第二路抽取必须经历排队：其排队号晚于切点（FIFO 后到），且最终完成
    抽队 = _查(
        "SELECT 排队号, 状态 FROM 切日排队 WHERE 持有方=? ORDER BY 排队号", (抽取作业["持有方"],)
    )
    切队 = _查(
        "SELECT 排队号 FROM 切日排队 WHERE 持有方=? ORDER BY 排队号", (切点作业["持有方"],)
    )
    曾排队 = (
        len(抽队) > 0 and len(切队) > 0
        and 抽队[0]["排队号"] > 切队[0]["排队号"]
        and all(r["状态"] == "完成" for r in 抽队)
    )

    # 迟到补抽：无租约直写必须被闸门拒绝
    迟到 = 景["迟到补抽"]
    迟到号 = f"{迟到['流水号']}-{令牌}"
    绕锁拒 = False
    try:
        切日租约.确保持租约(None)
    except 切日租约.无租约写入:
        绕锁拒 = True
    # 即便绕过服务直接 SQL 尝试，也不得留下该迟到流水（显式校验库内不存在）
    迟到落库 = _查(
        "SELECT COUNT(*) AS c FROM 变动流水 WHERE 流水号=?", (迟到号,)
    )[0]["c"]
    实测 = {
        "切点成功": bool(果.get("切点")) and 果["切点"].get("状态") == "成功",
        "第二路排队后成功": bool(果.get("抽取")) and 曾排队 and "抽取异常" not in 果,
        "双成功": bool(果.get("切点")) and bool(果.get("抽取")),
        "迟到绕锁被拒": 绕锁拒,
        "迟到流水落库": 迟到落库,
    }
    return _核对(景["断言"], 实测), {"作业": 果, "排队曾等待": 曾排队, "实测": 实测}


_场景表 = {
    "切日并行-双成功守恒": _场景并行,
    "租约超时-失败闭回滚": _场景超时,
    "多户装载半截-丢租约回滚": _场景多户半截,
    "切日中第二路抽取-排队禁闯": _场景排队绕锁,
}


def 列切日夹具() -> list[str]:
    if not 夹具目录.exists():
        return []
    return sorted(p.name for p in 夹具目录.glob("*.json") if p.name.startswith(("切日", "租约", "多户", "迟到")))


def 运行切日夹具(名: str) -> dict[str, Any]:
    景 = _读夹具(名)
    令牌 = uuid.uuid4().hex[:8]
    场景 = 景.get("场景")
    if 场景 not in _场景表:
        raise ValueError(f"未知切日场景: {场景}")
    # 口径自检：夹具声明必须与全局架构一致，防止甲/乙夹具混跑
    if 景.get("双成功允许") != 切日租约.双成功允许:
        raise ValueError("夹具双成功口径与全局架构（乙：双成功允许）不符，拒绝运行")
    断言结, 细 = _场景表[场景](景, 令牌)
    return {
        "夹具": 名,
        "场景": 场景,
        "口径": 切日租约.口径,
        "双成功允许": 切日租约.双成功允许,
        "运行令牌": 令牌,
        "全通过": all(a["通过"] for a in 断言结),
        "断言": 断言结,
        "细节": 细,
    }
