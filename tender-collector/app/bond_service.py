"""保证金核验编排：已采集公告(DB) + 原公告详情 + 保证金平台分页明细。

与采集接口不同，核验不写入数据库：每次请求实时读取原公告详情页与保证金
明细目录，返回目标标段的有效金额、缺口与逐笔来源。

错误语义：
- 输入引用的原公告/标段在本地采集数据中不存在 -> BondInputError（404）；
- 原公告详情页、明细分页读取失败或无法解析 -> BondIndeterminate，
  API 仍返回 200，status=indeterminate，绝不把读不全的账目判成“不足”；
- 账目结构冲突（悬空/超额/同编号冲突）同样返回 indeterminate。
"""
from __future__ import annotations

from . import database
from .bond import BondVerifyError, verify_bond
from .merge import merge_projects
from .scraper import (
    BondRequirement,
    CollectionError,
    fetch_bond_catalog,
    fetch_bond_requirement,
)


class BondInputError(Exception):
    """请求输入无法在已采集数据中定位（HTTP 404）。"""


class BondIndeterminate(Exception):
    """外部页面读取/解析失败，无法判定（HTTP 200 + indeterminate）。"""

    def __init__(self, reasons: str | list[str]):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


def _resolve_target(announcement_code: str, lot: str) -> dict:
    """从已采集公告还原生效截止时间，并定位原公告详情 URL。"""
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_announcement_group(conn, announcement_code)]
    if not rows or not any(r["code"] == announcement_code and r["kind"] == "original" for r in rows):
        raise BondInputError(
            f"原公告 {announcement_code} 尚未采集，请先调用 /api/collect"
        )

    original = next(r for r in rows if r["code"] == announcement_code)
    try:
        effective = merge_projects(rows)
    except Exception as exc:  # 归并层的数据完整性问题：无法判定而非输入错误
        raise BondIndeterminate(f"生效截止时间无法确定: {exc}") from exc
    current = next((d for d in effective if d.project_name == original["project_name"]), None)
    if current is None or not current.deadline:
        raise BondIndeterminate(
            f"原公告 {announcement_code} 及其更正公告均未给出投标截止时间"
        )
    return {
        "detail_url": original["detail_url"],
        "deadline": current.deadline,
        "deadline_source_code": current.source_code,
        "deadline_source_url": current.source_url,
    }


def verify(
    *, announcement_code: str, lot: str, enterprise_id: str, catalog_url: str
) -> dict:
    target = _resolve_target(announcement_code, lot)

    # 1) 原公告详情：应缴金额 + 收款账户（读失败 -> 无法判定）
    try:
        requirement: BondRequirement = fetch_bond_requirement(
            target["detail_url"], announcement_code
        )
    except CollectionError as exc:
        raise BondIndeterminate(str(exc)) from exc

    lot_spec = next((l for l in requirement.lots if l.lot == lot), None)
    if lot_spec is None:
        raise BondInputError(
            f"原公告 {announcement_code} 不存在标段 {lot}，"
            f"可用标段: {', '.join(l.lot for l in requirement.lots)}"
        )

    # 2) 保证金平台明细分页（任一页面读取失败 -> 无法判定）
    try:
        records = fetch_bond_catalog(catalog_url)
    except CollectionError as exc:
        raise BondIndeterminate(str(exc)) from exc

    # 3) 纯逻辑核验：结构冲突 -> indeterminate；业务过滤 -> verified
    try:
        result = verify_bond(
            announcement_code=announcement_code,
            lot=lot,
            enterprise_id=enterprise_id,
            required_amount=lot_spec.required_amount,
            account_no=lot_spec.account_no,
            deadline_text=target["deadline"],
            records=records,
        )
    except BondVerifyError as exc:
        raise BondIndeterminate(str(exc)) from exc

    payload = result.as_dict()
    payload.update(
        {
            "announcement_code": announcement_code,
            "lot": lot,
            "enterprise_id": enterprise_id,
            "target_lot": f"{announcement_code}/{lot}",
            "deadline": target["deadline"],
            "deadline_source_code": target["deadline_source_code"],
            "deadline_source_url": target["deadline_source_url"],
            "account_no": lot_spec.account_no,
            "catalog_url": catalog_url,
            "announcement_detail_url": requirement.detail_url,
        }
    )
    return payload
