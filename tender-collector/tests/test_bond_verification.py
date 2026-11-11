"""保证金核验端到端测试。

用线程内静态 HTTP 服务器托管示例站（公告站 + 保证金平台明细分页），
通过 FastAPI TestClient 走完整流程：采集公告 -> 调用 /api/bond/verify。
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"

spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    fail_paths: set[str] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def demo_server():
    handler = functools.partial(FlakyHandler)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "bond.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


def _collect(c, base):
    resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _verify(c, base, code, lot, ent, catalog="bond_1.html"):
    return c.post(
        "/api/bond/verify",
        json={
            "announcement_code": code,
            "lot": lot,
            "enterprise_id": ent,
            "catalog_url": f"{base}/{catalog}",
        },
    )


# ---------------------------------------------------------------------------
# 足额 / 不足 / 冲正 / 错账户 / 跨标段
# ---------------------------------------------------------------------------
def test_sufficient_with_cross_lot_allocation(client, demo_server):
    """E1001：80000 足额；一笔到账跨标段分配，超额约束按实收金额生效。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    d = _verify(c, base, "SZ-2026-001", "G1", "E1001").json()
    assert d["status"] == "verified"
    assert d["required_amount"] == "80000.00"
    assert d["valid_amount"] == "80000.00"
    assert d["gap"] == "0.00"
    assert d["is_sufficient"] is True
    assert d["deadline"] == "2026-11-25 14:30"          # 更正后的截止时间
    assert d["deadline_source_code"] == "SZ-2026-001-G1"
    assert d["account_no"] == gen.ACC_MAIN[1]

    ids = {s["allocation_id"] for s in d["sources"]}
    assert ids == {"ALC-2026-0001", "ALC-2026-0002"}
    # 逐笔来源必须带回到账引用
    by_alloc = {s["allocation_id"]: s for s in d["sources"]}
    assert by_alloc["ALC-2026-0001"]["arrival_id"] == "ARR-2026-0001"
    assert by_alloc["ALC-2026-0002"]["arrival_id"] == "ARR-2026-0002"

    # 跨标段分配被排除在目标标段之外，但保留可追溯记录
    cross = [e for e in d["excluded"] if e["record_id"] == "ALC-2026-0003"]
    assert len(cross) == 1 and "其他标段" in cross[0]["reason"]


def test_reversal_before_deadline_voids_arrival(client, demo_server):
    """E1002：截止前冲正，到账及其分配整体剔除 -> 真实不足，而非无法判定。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    d = _verify(c, base, "SZ-2026-001", "G1", "E1002").json()
    assert d["status"] == "verified"
    assert d["valid_amount"] == "30000.00"
    assert d["gap"] == "50000.00"
    assert d["is_sufficient"] is False

    excluded_ids = {e["record_id"] for e in d["excluded"]}
    # 冲正本身、被冲正的到账、挂在该到账上的分配都不计入
    assert {"REV-2026-0001", "ARR-2026-0003", "ALC-2026-0004"} <= excluded_ids
    assert {s["allocation_id"] for s in d["sources"]} == {"ALC-2026-0005"}


def test_reversal_after_deadline_keeps_funds(client, demo_server):
    """E1003：冲正受理于截止之后，截止前资金仍有效 -> 足额。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    d = _verify(c, base, "SZ-2026-004", "G1", "E1003").json()
    assert d["status"] == "verified"
    assert d["valid_amount"] == "30000.00"
    assert d["gap"] == "0.00"
    assert d["is_sufficient"] is True
    rev = next(e for e in d["excluded"] if e["record_id"] == "REV-2026-0002")
    assert "截止后冲正" in rev["reason"]


def test_wrong_account_not_counted(client, demo_server):
    """E1004：汇入非公告指定账户的款项全部不计入 -> 有效金额 0、缺口全额。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    d = _verify(c, base, "SZ-2026-004", "G1", "E1004").json()
    assert d["status"] == "verified"
    assert d["valid_amount"] == "0.00"
    assert d["gap"] == "30000.00"
    assert d["is_sufficient"] is False
    assert d["sources"] == []
    reasons = " ".join(e["reason"] for e in d["excluded"])
    assert "非公告指定账户" in reasons


def test_multi_lot_project_g1_sufficient_g2_short(client, demo_server):
    """E1005：同一企业跨两个标段；G1 足额，G2 有效 50000、缺口 70000。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    g1 = _verify(c, base, "SZ-2026-002", "G1", "E1005").json()
    assert g1["status"] == "verified"
    assert g1["valid_amount"] == "50000.00" and g1["gap"] == "0.00"

    g2 = _verify(c, base, "SZ-2026-002", "G2", "E1005").json()
    assert g2["status"] == "verified"
    assert g2["required_amount"] == "120000.00"
    assert g2["valid_amount"] == "50000.00"
    assert g2["gap"] == "70000.00"
    assert g2["is_sufficient"] is False

    excluded = {e["record_id"]: e["reason"] for e in g2["excluded"]}
    # 错账户（老专户）到账及其分配不计入
    assert "非公告指定账户" in excluded["ARR-2026-0008"]
    # 截止后到账/分配不计入
    assert "晚于截止时间" in excluded["ARR-2026-0009"]
    assert "晚于截止时间" in excluded["ALC-2026-0011"]
    # G1 的分配不计入 G2
    assert "其他标段" in excluded["ALC-2026-0008"]


def test_different_designated_accounts_per_lot(client, demo_server):
    """E1007：G1 指定专户、G2 指定老专户，按各标段账户分别判定。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    g1 = _verify(c, base, "SZ-2026-005", "G1", "E1007").json()
    assert g1["status"] == "verified"
    assert g1["account_no"] == gen.ACC_MAIN[1]
    assert g1["valid_amount"] == "80000.00" and g1["gap"] == "20000.00"

    g2 = _verify(c, base, "SZ-2026-005", "G2", "E1007").json()
    assert g2["status"] == "verified"
    assert g2["account_no"] == gen.ACC_OLD[1]
    assert g2["valid_amount"] == "60000.00" and g2["gap"] == "0.00"
    assert g2["is_sufficient"] is True


def test_expired_project_uses_historical_records(client, demo_server):
    """E1006：已过期项目仍可核验历史账目，受理时间在截止前即有效。"""
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    d = _verify(c, base, "SZ-2026-003", "G1", "E1006").json()
    assert d["status"] == "verified"
    assert d["deadline"] == "2026-10-30 17:00"
    assert d["valid_amount"] == "20000.00" and d["is_sufficient"] is True
    assert d["excluded"] == []


# ---------------------------------------------------------------------------
# 无法判定：页面失败 + 残缺账目（绝不判成不足）
# ---------------------------------------------------------------------------
def test_bond_page_failure_is_indeterminate(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server
    _collect(c, base)

    handler.fail_paths = {"/bond_3.html"}
    try:
        resp = _verify(c, base, "SZ-2026-001", "G1", "E1001")
        assert resp.status_code == 200
        d = resp.json()
        assert d["status"] == "indeterminate"
        assert d["is_sufficient"] is None and d["valid_amount"] is None
        assert d["gap"] is None and d["sources"] == []
        assert any("bond_3" in r for r in d["reasons"])
    finally:
        handler.fail_paths = set()


def test_announcement_detail_failure_is_indeterminate(client, demo_server):
    c, _, _ = client
    base, httpd, handler = demo_server
    _collect(c, base)

    handler.fail_paths = {"/SZ-2026-001.html"}
    try:
        resp = _verify(c, base, "SZ-2026-001", "G1", "E1001")
        assert resp.status_code == 200
        d = resp.json()
        assert d["status"] == "indeterminate"
        assert d["is_sufficient"] is None
        assert any("SZ-2026-001" in r for r in d["reasons"])
    finally:
        handler.fail_paths = set()


@pytest.mark.parametrize(
    "catalog, keyword",
    [
        ("bond_dangling.html", "引用悬空"),
        ("bond_overallocation.html", "分配超额"),
        ("bond_conflict.html", "同编号明细冲突"),
    ],
)
def test_broken_catalogs_are_indeterminate(client, demo_server, catalog, keyword):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _verify(c, base, "SZ-2026-001", "G1", "E1001", catalog=catalog)
    assert resp.status_code == 200
    d = resp.json()
    assert d["status"] == "indeterminate"
    # 残缺账目绝不能输出“不足”结论
    assert d["is_sufficient"] is None
    assert d["valid_amount"] is None and d["gap"] is None
    assert any(keyword in r for r in d["reasons"])


def test_unknown_announcement_and_lot_return_404(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    _collect(c, base)

    resp = _verify(c, base, "SZ-2099-999", "G1", "E1001")
    assert resp.status_code == 404
    assert "尚未采集" in resp.json()["detail"]

    resp = _verify(c, base, "SZ-2026-001", "GX", "E1001")
    assert resp.status_code == 404
    assert "不存在标段 GX" in resp.json()["detail"]


def test_verify_does_not_write_database(client, demo_server):
    """核验是只读操作：不新增公告、不产生采集记录。"""
    c, _, database = client
    base, _, _ = demo_server
    _collect(c, base)

    with database.get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        runs_before = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]

    _verify(c, base, "SZ-2026-001", "G1", "E1001")
    _verify(c, base, "SZ-2026-002", "G2", "E1005")

    with database.get_conn() as conn:
        after = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        runs_after = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert before == after == 10
    assert runs_before == runs_after
