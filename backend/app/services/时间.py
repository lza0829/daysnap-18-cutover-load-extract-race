from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 单调逻辑时钟（租约时间基准）
# 生产默认走 time.monotonic()；测试可通过 置时钟() 注入确定性时钟（如手动跳变）。
# 禁止以各进程私有锁做互斥，时钟仅用于判定「库内租约实体」的截止，不承担互斥。
# ---------------------------------------------------------------------------
_时钟源 = None


def 置时钟(源=None):
    """注入返回单调秒数（float）的可调用对象；传 None 恢复系统时钟。"""
    global _时钟源
    _时钟源 = 源


def 单调秒() -> float:
    if _时钟源 is not None:
        return float(_时钟源())
    import time

    return time.monotonic()


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 标准化营业日(值: str) -> str:
    值 = (值 or "").strip()
    if len(值) >= 10:
        return 值[:10]
    raise ValueError("营业日格式须为 YYYY-MM-DD")
