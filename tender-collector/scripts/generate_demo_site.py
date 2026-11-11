#!/usr/bin/env python3
"""生成本地分页 HTML 示例站（市政工程招投标公告 + 保证金平台明细）。

生成结果写入 demo_site/，纯静态文件，可用任意 HTTP 服务器托管：
    python3 -m http.server 8001 --directory demo_site

包含两部分：

一、公告站（list_*.html + 公告详情页）
数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的“生效截止时间”由发布时间最新的那份公告（原公告或更正公告）决定；
- 含已过期与未过期两种项目。

二、保证金平台明细站（bond_*.html）
沿 nav.pagination 分页，明细分三类：到账(arrival)、冲正(reversal)、分配(allocation)，
每条明细带受理时间；分配引用到账编号。覆盖：
- 足额 / 真实不足 / 到账在截止后 / 截止前冲正（到账与分配整体剔除）
- 截止后冲正（不影响截止前资金）/ 汇入非指定账户（不计入）
- 同一笔到账跨标段分配（总额受实收金额约束）
- 明细在分页间乱序排列（判定不能依赖页面顺序）
另有三个单页“残缺账目”目录，分别制造引用悬空、分配超额、同编号冲突。
"""
from __future__ import annotations

import html
import random
from dataclasses import dataclass
from pathlib import Path

PAGE_SIZE = 4
BOND_PAGE_SIZE = 6
OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site"


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 保证金相关示例数据
# ---------------------------------------------------------------------------
# 公告指定的两个收款账户 + 一个错误账户
ACC_MAIN = ("市政建设工程交易中心投标保证金专户", "8000100020260001", "中国建设银行市政支行")
ACC_OLD = ("市政建设工程交易中心保证金老专户", "8000100020250999", "中国工商银行市政支行")
ACC_WRONG = ("供应商一般结算户", "6222021234000099", "招商银行城东支行")


@dataclass(frozen=True)
class LotSpec:
    lot: str                       # 标段号
    lot_name: str                  # 标段名称
    amount: str                    # 应缴保证金金额（元）
    account: tuple[str, str, str]  # (账户名称, 账号, 开户银行)


# 原公告编号 -> 标段保证金要求（仅原公告承载保证金信息）
BOND_LOTS: dict[str, list[LotSpec]] = {
    "SZ-2026-001": [
        LotSpec("G1", "滨河路市政改造工程施工一标段", "80000.00", ACC_MAIN),
    ],
    "SZ-2026-002": [
        LotSpec("G1", "城北污水处理厂扩建土建标段", "50000.00", ACC_MAIN),
        LotSpec("G2", "城北污水处理厂扩建安装标段", "120000.00", ACC_MAIN),
    ],
    "SZ-2026-003": [
        LotSpec("G1", "东风桥加固维修工程施工标段", "20000.00", ACC_MAIN),
    ],
    "SZ-2026-004": [
        LotSpec("G1", "高新区雨污分流管网工程一标段", "30000.00", ACC_MAIN),
    ],
    "SZ-2026-005":
        # 同一项目不同标段指定不同收款账户
        [
        LotSpec("G1", "湖滨公园配套道路工程路基标段", "100000.00", ACC_MAIN),
        LotSpec("G2", "湖滨公园配套道路工程路面标段", "60000.00", ACC_OLD),
    ],
}


@dataclass
class BondRecord:
    """保证金平台明细：到账 / 冲正 / 分配。"""

    record_id: str
    kind: str                       # arrival | reversal | allocation
    enterprise_id: str
    enterprise_name: str
    accepted_at: str                # 受理时间 YYYY-MM-DD HH:MM
    amount: str
    account_no: str | None = None   # 到账/冲正的收款账号
    reverses_id: str | None = None  # 冲正引用的到账编号
    arrival_id: str | None = None   # 分配引用的到账编号
    lot: str | None = None          # 分配目标标段（公告编号/标段号）
    remark: str = ""

    @property
    def filename_anchor(self) -> str:
        return f"#bond-{self.record_id}"


ENTERPRISES = {
    "E1001": "城安建设集团有限公司",
    "E1002": "宏通路桥工程有限公司",
    "E1003": "远东市政工程有限公司",
    "E1004": "润泽管网建设有限公司",
    "E1005": "滨港建设股份有限公司",
    "E1006": "金桥建设发展有限公司",
    "E1007": "中汇环境工程有限公司",
}


def _arrival(rid, ent, ts, amount, account_no, remark="") -> BondRecord:
    return BondRecord(rid, "arrival", ent, ENTERPRISES[ent], ts, amount,
                      account_no=account_no, remark=remark)


def _reversal(rid, ent, ts, amount, account_no, reverses_id, remark="") -> BondRecord:
    return BondRecord(rid, "reversal", ent, ENTERPRISES[ent], ts, amount,
                      account_no=account_no, reverses_id=reverses_id, remark=remark)


def _allocation(rid, ent, ts, amount, arrival_id, lot, remark="") -> BondRecord:
    return BondRecord(rid, "allocation", ent, ENTERPRISES[ent], ts, amount,
                      arrival_id=arrival_id, lot=lot, remark=remark)


# 主目录：账目自洽的 28 条明细（跨 5 个分页，渲染时再打乱顺序）
BOND_RECORDS: list[BondRecord] = [
    # --- E1001 -> SZ-2026-001/G1（截止 2026-11-25 14:30，应缴 80000）：足额，
    #     第二笔到账跨标段分配给 002/G1，验证“同一笔到账分配总额 ≤ 实收” ---
    _arrival("ARR-2026-0001", "E1001", "2026-11-10 09:15", "50000.00", ACC_MAIN[1]),
    _arrival("ARR-2026-0002", "E1001", "2026-11-12 14:05", "40000.00", ACC_MAIN[1]),
    _allocation("ALC-2026-0001", "E1001", "2026-11-12 15:00", "50000.00",
                "ARR-2026-0001", "SZ-2026-001/G1"),
    _allocation("ALC-2026-0002", "E1001", "2026-11-12 15:02", "30000.00",
                "ARR-2026-0002", "SZ-2026-001/G1"),
    _allocation("ALC-2026-0003", "E1001", "2026-11-12 15:04", "10000.00",
                "ARR-2026-0002", "SZ-2026-002/G1", "同一笔到账跨标段分配"),

    # --- E1002 -> SZ-2026-001/G1：截止前冲正，到账及其分配整体剔除，真实不足 ---
    _arrival("ARR-2026-0003", "E1002", "2026-11-08 10:00", "60000.00", ACC_MAIN[1]),
    _reversal("REV-2026-0001", "E1002", "2026-11-09 11:00", "60000.00",
              ACC_MAIN[1], "ARR-2026-0003", "企业汇错项目，截止前冲正"),
    _arrival("ARR-2026-0004", "E1002", "2026-11-11 09:40", "30000.00", ACC_MAIN[1]),
    _allocation("ALC-2026-0004", "E1002", "2026-11-11 10:20", "60000.00",
                "ARR-2026-0003", "SZ-2026-001/G1"),
    _allocation("ALC-2026-0005", "E1002", "2026-11-11 10:22", "30000.00",
                "ARR-2026-0004", "SZ-2026-001/G1"),

    # --- E1003 -> SZ-2026-004/G1（截止 2026-11-28 09:30，应缴 30000）：
    #     冲正受理于截止之后，截止前资金仍然有效，足额 ---
    _arrival("ARR-2026-0005", "E1003", "2026-11-15 13:20", "30000.00", ACC_MAIN[1]),
    _reversal("REV-2026-0002", "E1003", "2026-11-30 10:00", "30000.00",
              ACC_MAIN[1], "ARR-2026-0005", "截止后发起的冲正"),
    _allocation("ALC-2026-0006", "E1003", "2026-11-15 14:00", "30000.00",
                "ARR-2026-0005", "SZ-2026-004/G1"),

    # --- E1004 -> SZ-2026-004/G1：汇入非公告指定账户，全部不计入，真实不足 ---
    _arrival("ARR-2026-0006", "E1004", "2026-11-16 09:30", "30000.00",
             ACC_WRONG[1], "误入供应商一般结算户"),
    _allocation("ALC-2026-0007", "E1004", "2026-11-16 10:00", "30000.00",
                "ARR-2026-0006", "SZ-2026-004/G1"),

    # --- E1005 -> SZ-2026-002（截止 2026-11-30 10:00）：
    #     G1 足额；G2 含错账户分配与截止后到账/分配，有效 50000，缺口 70000 ---
    _arrival("ARR-2026-0007", "E1005", "2026-11-14 10:00", "100000.00", ACC_MAIN[1]),
    _arrival("ARR-2026-0008", "E1005", "2026-11-18 15:30", "50000.00",
             ACC_OLD[1], "汇入老专户"),
    _arrival("ARR-2026-0009", "E1005", "2026-12-02 09:00", "20000.00",
             ACC_MAIN[1], "截止后到账"),
    _allocation("ALC-2026-0008", "E1005", "2026-11-14 11:00", "50000.00",
                "ARR-2026-0007", "SZ-2026-002/G1"),
    _allocation("ALC-2026-0009", "E1005", "2026-11-14 11:05", "50000.00",
                "ARR-2026-0007", "SZ-2026-002/G2"),
    _allocation("ALC-2026-0010", "E1005", "2026-11-18 16:00", "50000.00",
                "ARR-2026-0008", "SZ-2026-002/G2"),
    _allocation("ALC-2026-0011", "E1005", "2026-12-02 09:30", "20000.00",
                "ARR-2026-0009", "SZ-2026-002/G2", "截止后分配"),

    # --- E1006 -> SZ-2026-003/G1（截止 2026-10-30 17:00，已过期项目）：
    #     历史账目，受理时间均在截止前，足额 ---
    _arrival("ARR-2026-0010", "E1006", "2026-10-25 10:00", "20000.00", ACC_MAIN[1]),
    _allocation("ALC-2026-0012", "E1006", "2026-10-25 11:00", "20000.00",
                "ARR-2026-0010", "SZ-2026-003/G1"),

    # --- E1007 -> SZ-2026-005（截止 2026-11-20 15:00）：
    #     G1 应缴 100000 仅有效 80000，缺口 20000；
    #     G2 指定老专户，60000 足额（同站不同账户，不能一刀切） ---
    _arrival("ARR-2026-0011", "E1007", "2026-11-13 10:00", "80000.00", ACC_MAIN[1]),
    _arrival("ARR-2026-0012", "E1007", "2026-11-14 10:00", "60000.00", ACC_OLD[1]),
    _allocation("ALC-2026-0013", "E1007", "2026-11-13 11:00", "80000.00",
                "ARR-2026-0011", "SZ-2026-005/G1"),
    _allocation("ALC-2026-0014", "E1007", "2026-11-14 11:00", "60000.00",
                "ARR-2026-0012", "SZ-2026-005/G2"),
]


# 三个“残缺账目”单页目录：用于无法判定场景的端到端测试
BROKEN_CATALOGS: dict[str, list[BondRecord]] = {
    # 分配引用了不存在的到账（引用悬空）
    "bond_dangling": [
        _arrival("ARR-9001", "E1001", "2026-11-10 09:00", "1000.00", ACC_MAIN[1]),
        _allocation("ALC-9001", "E1001", "2026-11-10 10:00", "1000.00",
                    "ARR-9001", "SZ-2026-001/G1"),
        _allocation("ALC-9002", "E1001", "2026-11-10 10:05", "500.00",
                    "ARR-9999", "SZ-2026-001/G1", "悬空引用"),
    ],
    # 同一笔到账被分配的总额超过实收金额（分配超额）
    "bond_overallocation": [
        _arrival("ARR-9101", "E1001", "2026-11-10 09:00", "100.00", ACC_MAIN[1]),
        _allocation("ALC-9101", "E1001", "2026-11-10 10:00", "100.00",
                    "ARR-9101", "SZ-2026-001/G1"),
        _allocation("ALC-9102", "E1001", "2026-11-10 10:01", "50.00",
                    "ARR-9101", "SZ-2026-002/G1", "超额分配"),
    ],
    # 同编号明细冲突（两条 record_id 相同但内容不同）
    "bond_conflict": [
        _arrival("ARR-9201", "E1001", "2026-11-10 09:00", "100.00", ACC_MAIN[1]),
        _arrival("ARR-9201", "E1002", "2026-11-11 09:00", "200.00", ACC_MAIN[1],
                 "重复编号"),
        _allocation("ALC-9201", "E1001", "2026-11-10 10:00", "100.00",
                    "ARR-9201", "SZ-2026-001/G1"),
    ],
}


# ---------------------------------------------------------------------------
# 公告数据：5 个项目，其中 3 个含更正公告，共 10 条公告
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
    ),
]


# ---------------------------------------------------------------------------
# 公告详情页 / 列表页渲染
# ---------------------------------------------------------------------------
def _bond_section(a: Announcement) -> str:
    """原公告详情页内嵌的投标保证金信息（应缴金额按标段 + 收款账户）。"""
    lots = BOND_LOTS.get(a.code)
    if not lots:
        return ""
    head = (
        '    <section class="bond-section" data-bid-bond="true">\n'
        "      <h2>投标保证金缴纳信息</h2>\n"
        "      <table class=\"bond-lots\">\n"
        "        <thead>\n"
        "          <tr>"
        "<th>标段号</th><th>标段名称</th><th>应缴保证金金额（元）</th>"
        "<th>收款账户名称</th><th>收款账号</th><th>开户银行</th>"
        "</tr>\n        </thead>\n        <tbody>\n"
    )
    rows = []
    for spec in lots:
        account_name, account_no, bank = spec.account
        rows.append(
            '          <tr class="lot-row" data-lot="{lot}" data-amount="{amount}" '
            'data-account-no="{account_no}">\n'
            '            <td data-field="标段号">{lot}</td>\n'
            '            <td data-field="标段名称">{lot_name}</td>\n'
            '            <td data-field="应缴金额">{amount}</td>\n'
            '            <td data-field="收款账户名称">{account_name}</td>\n'
            '            <td data-field="收款账号">{account_no}</td>\n'
            '            <td data-field="开户银行">{bank}</td>\n'
            "          </tr>".format(
                lot=html.escape(spec.lot),
                lot_name=html.escape(spec.lot_name),
                amount=html.escape(spec.amount),
                account_name=html.escape(account_name),
                account_no=html.escape(account_no),
                bank=html.escape(bank),
            )
        )
    return head + "\n".join(rows) + "\n        </tbody>\n      </table>\n    </section>\n"


def _detail_page(a: Announcement) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    bond_section = _bond_section(a) if a.kind == "original" else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
{bond_section}    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def _list_page(items: list[Announcement], page: int, total_pages: int) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 保证金平台明细页渲染
# ---------------------------------------------------------------------------
KIND_LABEL = {"arrival": "到账", "reversal": "冲正", "allocation": "标段分配"}


def _bond_record_li(r: BondRecord, page_url: str) -> str:
    attrs = [
        f'data-record-id="{html.escape(r.record_id)}"',
        f'data-kind="{r.kind}"',
        f'data-enterprise-id="{html.escape(r.enterprise_id)}"',
        f'data-enterprise-name="{html.escape(r.enterprise_name)}"',
        f'data-accepted-at="{html.escape(r.accepted_at)}"',
        f'data-amount="{html.escape(r.amount)}"',
    ]
    if r.account_no is not None:
        attrs.append(f'data-account-no="{html.escape(r.account_no)}"')
    if r.reverses_id is not None:
        attrs.append(f'data-reverses-id="{html.escape(r.reverses_id)}"')
    if r.arrival_id is not None:
        attrs.append(f'data-arrival-id="{html.escape(r.arrival_id)}"')
    if r.lot is not None:
        attrs.append(f'data-lot="{html.escape(r.lot)}"')
    detail = f"{page_url}#bond-{html.escape(r.record_id)}"
    return (
        f'      <li class="bond-record" id="bond-{html.escape(r.record_id)}"\n'
        f'          {" ".join(attrs)} data-detail-url="{html.escape(detail)}">\n'
        f'        <span class="record-kind">{KIND_LABEL[r.kind]}</span>\n'
        f'        <span class="record-id">{html.escape(r.record_id)}</span>\n'
        f'        <span class="enterprise">{html.escape(r.enterprise_id)} '
        f'{html.escape(r.enterprise_name)}</span>\n'
        f'        <span class="accepted-at">{html.escape(r.accepted_at)}</span>\n'
        f'        <span class="amount">{html.escape(r.amount)}</span>\n'
        + (f'        <span class="remark">{html.escape(r.remark)}</span>\n' if r.remark else "")
        + "      </li>"
    )


def _bond_page(records: list[BondRecord], page: int, total_pages: int,
               filename_prefix: str, title: str) -> str:
    lis = "\n".join(_bond_record_li(r, f"{filename_prefix}_{page}.html") for r in records)
    if total_pages > 1:
        nav_parts = []
        for p in range(1, total_pages + 1):
            if p == page:
                nav_parts.append(f'<span class="page-current">{p}</span>')
            else:
                nav_parts.append(
                    f'<a class="page-link" href="{filename_prefix}_{p}.html">{p}</a>'
                )
        next_link = (
            f'<a class="page-next" href="{filename_prefix}_{page + 1}.html">下一页</a>'
            if page < total_pages
            else '<span class="page-next-disabled">下一页</span>'
        )
        pagination = (
            '  <nav class="pagination">\n    '
            + "".join(nav_parts)
            + "\n    "
            + next_link
            + "\n  </nav>\n"
        )
    else:
        pagination = '  <nav class="pagination"><span class="page-current">1</span></nav>\n'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)} - 第 {page} 页</title>
</head>
<body>
  <h1>{html.escape(title)}（第 {page}/{total_pages} 页）</h1>
  <ul class="bond-record-list" data-page="{page}">
{lis}
  </ul>
{pagination}</body>
</html>
"""


def _write_bond_catalog(prefix: str, records: list[BondRecord], title: str,
                        paginate: bool = True) -> None:
    if paginate:
        shuffled = list(records)
        # 固定随机种子：明细在分页间乱序，但生成结果可复现
        random.Random(20261111).shuffle(shuffled)
        total_pages = (len(shuffled) + BOND_PAGE_SIZE - 1) // BOND_PAGE_SIZE
        for page in range(1, total_pages + 1):
            chunk = shuffled[(page - 1) * BOND_PAGE_SIZE: page * BOND_PAGE_SIZE]
            (OUT_DIR / f"{prefix}_{page}.html").write_text(
                _bond_page(chunk, page, total_pages, prefix, title), encoding="utf-8"
            )
    else:
        (OUT_DIR / f"{prefix}.html").write_text(
            _bond_page(records, 1, 1, prefix, title), encoding="utf-8"
        )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(ANNOUNCEMENTS, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE

    for a in ordered:
        (OUT_DIR / a.filename).write_text(_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (OUT_DIR / f"list_{page}.html").write_text(
            _list_page(chunk, page, total_pages), encoding="utf-8"
        )

    (OUT_DIR / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…</body></html>\n",
        encoding="utf-8",
    )

    # 保证金平台明细：主目录分页 + 残缺账目单页目录
    _write_bond_catalog("bond", BOND_RECORDS, "保证金平台企业资金明细目录")
    for name, broken in BROKEN_CATALOGS.items():
        _write_bond_catalog(name, broken, f"保证金明细目录（{name}）", paginate=False)

    print(
        f"generated {len(ordered)} announcement pages, {total_pages} list pages; "
        f"{len(BOND_RECORDS)} bond records across "
        f"{(len(BOND_RECORDS) + BOND_PAGE_SIZE - 1) // BOND_PAGE_SIZE} bond pages; "
        f"{len(BROKEN_CATALOGS)} broken catalogs in {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
