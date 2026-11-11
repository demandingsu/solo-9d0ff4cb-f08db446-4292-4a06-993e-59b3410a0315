"""公告站采集与 HTML 解析。

流程：
1. 从用户提交的列表页开始，沿分页链接抓取全部列表页（去重、防环、限量）；
2. 收集列表页中全部详情页链接（同源、去重）；
3. 逐页抓取详情并解析为结构化公告。

任一列表页或详情页读取失败（网络错误、非 2xx、HTML 缺关键字段）都抛出
CollectionError，由上层整体放弃本次采集，数据库保留上次成功结果。

保证金核验相关的读取/解析也在本模块：
- fetch_bond_requirement：从原公告详情页读取各标段应缴金额与收款账户；
- fetch_bond_catalog：沿同站分页读取保证金平台的到账/冲正/分配明细。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from .config import settings


class CollectionError(Exception):
    """采集过程中任一环节失败。"""


@dataclass
class ParsedAnnouncement:
    code: str
    title: str
    kind: str
    project_name: str
    published_at: str
    deadline: str | None
    corrects_code: str | None
    detail_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "kind": self.kind,
            "project_name": self.project_name,
            "published_at": self.published_at,
            "deadline": self.deadline,
            "corrects_code": self.corrects_code,
            "detail_url": self.detail_url,
        }


def _same_origin(url: str, base: str) -> bool:
    u, b = urlparse(url), urlparse(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc)


def _get(client: httpx.Client, url: str) -> str:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        raise CollectionError(f"读取页面失败 {url}: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise CollectionError(f"读取页面失败 {url}: HTTP {resp.status_code}")
    return resp.text


def _parse_list(html_text: str, list_url: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    detail_urls: set[str] = set()
    page_urls: set[str] = set()

    for a in soup.select("ul.announcement-list a.detail-link[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            detail_urls.add(href)

    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            page_urls.add(href)

    if not detail_urls:
        raise CollectionError(f"列表页 {list_url} 未解析到任何详情链接")
    return detail_urls, page_urls


def _field(soup: BeautifulSoup, label: str) -> str | None:
    cell = soup.select_one(f'th[data-field="{label}"] + td')
    if cell is None:
        return None
    text = cell.get_text(strip=True)
    return text or None


def _parse_detail(html_text: str, detail_url: str) -> ParsedAnnouncement:
    soup = BeautifulSoup(html_text, "html.parser")
    article = soup.select_one("article.announcement-detail")
    if article is None:
        raise CollectionError(f"详情页 {detail_url} 缺少 article.announcement-detail")

    code = article.get("data-code") or _field(soup, "公告编号")
    kind_raw = article.get("data-kind")
    published = article.get("data-published-at") or _field(soup, "发布时间")
    corrects = article.get("data-corrects") or _field(soup, "原公告编号")

    title_el = soup.select_one("h1.detail-title")
    title = title_el.get_text(strip=True) if title_el else None
    project_name = _field(soup, "项目名称")
    deadline = _field(soup, "投标截止时间")

    kind = {"original": "original", "correction": "correction"}.get(kind_raw or "", "")
    if not kind:
        label = _field(soup, "公告类型") or ""
        kind = "correction" if "更正" in label else "original"

    missing = [
        name
        for name, val in (
            ("公告编号", code),
            ("公告标题", title),
            ("项目名称", project_name),
            ("发布时间", published),
        )
        if not val
    ]
    if missing:
        raise CollectionError(f"详情页 {detail_url} 缺少必填字段: {', '.join(missing)}")
    if kind == "correction" and not corrects:
        raise CollectionError(f"更正公告 {code}（{detail_url}）缺少原公告编号")

    return ParsedAnnouncement(
        code=code.strip(),
        title=title.strip(),
        kind=kind,
        project_name=project_name.strip(),
        published_at=published.strip(),
        deadline=deadline.strip() if deadline else None,
        corrects_code=corrects.strip() if corrects else None,
        detail_url=detail_url,
    )


def collect(list_url: str) -> list[dict[str, Any]]:
    """抓取整个公告站并返回结构化公告列表（顺序不保证，归并层不依赖顺序）。"""
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        # 1) 遍历全部列表页
        seen_pages: set[str] = set()
        queue: list[str] = [list_url]
        detail_urls: set[str] = set()

        while queue:
            if len(seen_pages) >= settings.max_list_pages:
                raise CollectionError(
                    f"列表页数量超过上限 {settings.max_list_pages}，疑似分页环"
                )
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            html_text = _get(client, page_url)
            details, more_pages = _parse_list(html_text, page_url)
            detail_urls.update(details)
            for u in more_pages:
                if u not in seen_pages and u not in queue:
                    queue.append(u)

        # 2) 逐页抓取详情 —— 任一失败即整体失败
        announcements: list[dict[str, Any]] = []
        for detail_url in sorted(detail_urls):  # 排序仅为日志/测试的确定性
            html_text = _get(client, detail_url)
            announcements.append(_parse_detail(html_text, detail_url).as_dict())

    return announcements


# ---------------------------------------------------------------------------
# 保证金核验：原公告保证金要求 + 平台明细分页采集
# ---------------------------------------------------------------------------
@dataclass
class LotRequirement:
    lot: str
    lot_name: str | None
    required_amount: Decimal
    account_name: str | None
    account_no: str
    bank: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lot": self.lot,
            "lot_name": self.lot_name,
            "required_amount": str(self.required_amount),
            "account_name": self.account_name,
            "account_no": self.account_no,
            "bank": self.bank,
        }


@dataclass
class BondRequirement:
    announcement_code: str
    detail_url: str
    lots: list[LotRequirement] = field(default_factory=list)


@dataclass
class BondRecordRow:
    record_id: str
    kind: str
    enterprise_id: str
    enterprise_name: str | None
    accepted_at: str
    amount: Decimal
    account_no: str | None
    reverses_id: str | None
    arrival_id: str | None
    lot: str | None
    detail_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "enterprise_id": self.enterprise_id,
            "enterprise_name": self.enterprise_name,
            "accepted_at": self.accepted_at,
            "amount": str(self.amount),
            "account_no": self.account_no,
            "reverses_id": self.reverses_id,
            "arrival_id": self.arrival_id,
            "lot": self.lot,
            "detail_url": self.detail_url,
        }


BOND_KINDS = {"arrival", "reversal", "allocation"}


def _parse_amount(raw: str | None, where: str) -> Decimal:
    if raw is None:
        raise CollectionError(f"{where} 缺少金额")
    text = raw.strip().replace(",", "").replace("，", "")
    for token in ("元", "人民币", "￥", "¥", " "):
        text = text.replace(token, "")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise CollectionError(f"{where} 金额无法解析: {raw!r}") from exc
    if amount < 0:
        raise CollectionError(f"{where} 金额为负数: {raw!r}")
    return amount


def _bond_field(row: Tag, label: str) -> str | None:
    cell = row.select_one(f'td[data-field="{label}"]')
    return cell.get_text(strip=True) if cell else None


def _parse_bond_requirement(html_text: str, detail_url: str,
                            announcement_code: str) -> BondRequirement:
    soup = BeautifulSoup(html_text, "html.parser")
    section = soup.select_one("section.bond-section")
    if section is None:
        raise CollectionError(
            f"原公告 {announcement_code}（{detail_url}）缺少保证金信息 section.bond-section"
        )

    lots: list[LotRequirement] = []
    seen_lots: set[str] = set()
    for row in section.select("tr.lot-row"):
        lot = (row.get("data-lot") or _bond_field(row, "标段号") or "").strip()
        amount_raw = row.get("data-amount") or _bond_field(row, "应缴金额")
        account_no = (
            row.get("data-account-no") or _bond_field(row, "收款账号") or ""
        ).strip()
        if not lot:
            raise CollectionError(f"{detail_url} 存在缺少标段号的保证金行")
        if lot in seen_lots:
            raise CollectionError(
                f"原公告 {announcement_code} 标段 {lot} 的保证金要求重复"
            )
        seen_lots.add(lot)
        if not account_no:
            raise CollectionError(
                f"原公告 {announcement_code} 标段 {lot} 缺少收款账号"
            )
        lots.append(
            LotRequirement(
                lot=lot,
                lot_name=_bond_field(row, "标段名称"),
                required_amount=_parse_amount(
                    amount_raw, f"原公告 {announcement_code} 标段 {lot}"
                ),
                account_name=_bond_field(row, "收款账户名称"),
                account_no=account_no,
                bank=_bond_field(row, "开户银行"),
            )
        )

    if not lots:
        raise CollectionError(
            f"原公告 {announcement_code}（{detail_url}）未给出任何标段的保证金要求"
        )
    return BondRequirement(
        announcement_code=announcement_code, detail_url=detail_url, lots=lots
    )


def _parse_bond_record(li: Tag, page_url: str) -> BondRecordRow:
    record_id = (li.get("data-record-id") or "").strip()
    kind = (li.get("data-kind") or "").strip()
    enterprise_id = (li.get("data-enterprise-id") or "").strip()
    enterprise_name = (li.get("data-enterprise-name") or "").strip() or None
    accepted_at = (li.get("data-accepted-at") or "").strip()
    amount_raw = li.get("data-amount")
    account_no = (li.get("data-account-no") or "").strip() or None
    reverses_id = (li.get("data-reverses-id") or "").strip() or None
    arrival_id = (li.get("data-arrival-id") or "").strip() or None
    lot = (li.get("data-lot") or "").strip() or None
    anchor = li.get("id")
    detail_url = (
        f"{page_url}#{anchor}" if anchor and not anchor.startswith("http") else page_url
    )

    where = f"保证金明细 {record_id or '<无编号>'}"
    missing = [
        name
        for name, val in (
            ("record_id", record_id),
            ("enterprise_id", enterprise_id),
            ("accepted_at", accepted_at),
        )
        if not val
    ]
    if missing:
        raise CollectionError(f"{where} 缺少必填字段: {', '.join(missing)}")
    if kind not in BOND_KINDS:
        raise CollectionError(f"{where} 的明细类型无法识别: {kind!r}")
    amount = _parse_amount(amount_raw, where)
    if kind == "reversal" and not reverses_id:
        raise CollectionError(f"{where} 冲正明细缺少 reverses 到账编号")
    if kind == "allocation" and not arrival_id:
        raise CollectionError(f"{where} 分配明细缺少引用的到账编号")
    if kind == "arrival" and not account_no:
        raise CollectionError(f"{where} 到账明细缺少收款账号")

    return BondRecordRow(
        record_id=record_id,
        kind=kind,
        enterprise_id=enterprise_id,
        enterprise_name=enterprise_name,
        accepted_at=accepted_at,
        amount=amount,
        account_no=account_no,
        reverses_id=reverses_id,
        arrival_id=arrival_id,
        lot=lot,
        detail_url=detail_url,
    )


def _parse_bond_page(html_text: str, page_url: str) -> tuple[list[BondRecordRow], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = soup.select("ul.bond-record-list li.bond-record")
    if not items:
        raise CollectionError(f"保证金明细页 {page_url} 未解析到任何明细")
    rows = [_parse_bond_record(li, page_url) for li in items]

    page_urls: set[str] = set()
    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(page_url, a["href"])
        if _same_origin(href, page_url):
            page_urls.add(href)
    return rows, page_urls


def _bond_client() -> httpx.Client:
    return httpx.Client(
        timeout=settings.request_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    )


def fetch_bond_requirement(detail_url: str, announcement_code: str) -> BondRequirement:
    """读取原公告详情页并解析各标段应缴金额与收款账户。"""
    with _bond_client() as client:
        html_text = _get(client, detail_url)
    return _parse_bond_requirement(html_text, detail_url, announcement_code)


def fetch_bond_catalog(catalog_url: str) -> list[dict[str, Any]]:
    """从保证金平台目录页出发，沿同站分页读取全部明细（页面去重、防环、限量）。

    分页间可能存在互相指向的链接（每页都列出全部页码），因此对页面 URL 去重；
    明细 record_id 不在这里去重——同一编号出现多条属于“同编号明细冲突”，
    必须原样上交给核验层标记为无法判定。
    """
    seen_pages: set[str] = set()
    queue: list[str] = [catalog_url]
    records: list[BondRecordRow] = []

    with _bond_client() as client:
        while queue:
            if len(seen_pages) >= settings.max_list_pages:
                raise CollectionError(
                    f"保证金明细分页数量超过上限 {settings.max_list_pages}，疑似分页环"
                )
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            html_text = _get(client, page_url)
            rows, more_pages = _parse_bond_page(html_text, page_url)
            records.extend(rows)
            for u in more_pages:
                if u not in seen_pages and u not in queue:
                    queue.append(u)

    return [r.as_dict() for r in records]
