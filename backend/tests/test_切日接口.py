"""切日互斥 HTTP 接口测试：核证、排队、切点、夹具运行、409 冲突口径。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import 切日租约


@pytest.fixture()
def 客户端(临时库):
    with TestClient(app) as c:
        yield c


def test_核证与排队接口(客户端):
    r = 客户端.get("/api/切日/核证")
    assert r.status_code == 200
    证 = r.json()
    assert 证["valid"] is False and 证["持有方"] is None
    assert 证["口径"] == "配对租约排队"
    assert 客户端.get("/api/切日/排队").status_code == 200
    assert 客户端.get("/api/切日/夹具").status_code == 200


def test_装载抽取回放经HTTP均带租约(客户端):
    r = 客户端.post("/api/装载夹具", json={"名": "三日连环样例.json"})
    assert r.status_code == 200 and r.json()["租约号"]
    r = 客户端.post("/api/抽取", json={"营业日": "2026-11-20"})
    assert r.status_code == 200 and r.json()["租约号"]
    r = 客户端.post("/api/回放", json={"营业日": "2026-11-20"})
    assert r.status_code == 200 and r.json()["通过"] is True


def test_切点接口与台账(客户端):
    r = 客户端.post("/api/切日/切点", json={"至营业日": "2026-11-25"})
    assert r.status_code == 200 and r.json()["状态"] == "成功"
    批 = 客户端.get("/api/切日批次").json()
    assert any(x["至营业日"] == "2026-11-25" and x["租约号"] for x in 批)


def test_排队超时返回409(客户端):
    # 长租约占住切日窗口；HTTP 请求以极短等待预算申请 → 路由映射 409，且未留业务写
    票 = 切日租约.申请租约("占座", "装载批", 租约秒=30, 等待秒=5)
    try:
        r = 客户端.post("/api/装载", json={
            "批次编号": "久等批",
            "流水": [{"流水号": "w1", "账户键": "户甲", "营业日": "2026-11-20", "变动额": 1}],
            "等待秒": 0.2,
        })
        assert r.status_code == 409
        # 久等方未取得写权：其流水不存在、无装载批次
        rows = 客户端.get("/api/流水?账户键=户甲").json()
        assert all(x["流水号"] != "w1" for x in rows)
        assert 客户端.get("/api/装载批次").json() == []
        # 撤销端点可提前释放占座租约
        撤 = 客户端.post("/api/切日/撤销", json={"租约号": 票["租约号"], "备注": "测试撤销"})
        assert 撤.status_code == 200 and 撤.json()["valid"] is False
    finally:
        切日租约.释放租约(票["租约号"])


def test_四夹具HTTP运行全通过(客户端):
    for 名 in 客户端.get("/api/切日/夹具").json():
        r = 客户端.post("/api/切日/运行夹具", json={"名": 名})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["全通过"] is True
        assert j["双成功允许"] is True  # 乙口径
