"""保证金核验纯逻辑：按受理时间与生效截止时间还原企业资金。

输入：
- 目标标段要求（应缴金额、公告指定收款账号、生效截止时间）；
- 保证金平台全量明细（到账 arrival / 冲正 reversal / 分配 allocation），
  分配与冲正都通过编号引用到账。

核验规则（与页面/输入顺序无关）：
1. 结构完整性（任何一条不满足，整体判为“无法判定”，绝不判成不足）：
   - 同一 record_id 出现多条明细（同编号冲突）；
   - 分配/冲正引用的到账不存在或不是到账类型（引用悬空）；
   - 分配引用了其他企业的到账（企业归属冲突）；
   - 同一笔到账被各标段分配的总额超过实收金额（分配超额）；
   - 受理时间无法解析。
2. 资金还原（以更正后的截止时间为界，按受理时间）：
   - 仅统计汇入公告指定收款账号的到账；
   - 到账/分配受理时间晚于截止时间的不计入；
   - 截止前已被冲正的到账及其全部分配不计入；截止后才冲正的不影响
     截止前已经形成的资金；
   - 目标标段的有效金额 = 满足以上条件且引用有效到账的分配金额之和；
   - 缺口 = 应缴金额 - 有效金额（不为负，最小为 0）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

TIME_FORMAT = "%Y-%m-%d %H:%M"
ZERO = Decimal("0.00")
Q = Decimal("0.01")


class BondVerifyError(ValueError):
    """核验输入本身不合法（区别于账目残缺导致的无法判定）。"""


@dataclass
class FundSource:
    """一笔计入目标标段的有效分配及其到账来源。"""

    allocation_id: str
    arrival_id: str
    allocation_accepted_at: str
    amount: Decimal
    arrival_accepted_at: str
    arrival_amount: Decimal
    account_no: str
    allocation_detail_url: str
    arrival_detail_url: str


@dataclass
class ExcludedItem:
    """未计入的明细及原因（逐笔可追溯）。"""

    record_id: str
    kind: str
    amount: Decimal | None
    reason: str
    detail_url: str


@dataclass
class VerificationResult:
    status: str                      # verified | indeterminate
    reasons: list[str] = field(default_factory=list)
    required_amount: Decimal | None = None
    valid_amount: Decimal | None = None
    gap: Decimal | None = None
    is_sufficient: bool | None = None
    sources: list[FundSource] = field(default_factory=list)
    excluded: list[ExcludedItem] = field(default_factory=list)
    arrival_count: int = 0
    reversal_count: int = 0
    allocation_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        def money(v: Decimal | None) -> str | None:
            return None if v is None else str(v.quantize(Q))

        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "required_amount": money(self.required_amount),
            "valid_amount": money(self.valid_amount),
            "gap": money(self.gap),
            "is_sufficient": self.is_sufficient,
            "sources": [
                {
                    "allocation_id": s.allocation_id,
                    "arrival_id": s.arrival_id,
                    "allocation_accepted_at": s.allocation_accepted_at,
                    "amount": str(s.amount.quantize(Q)),
                    "arrival_accepted_at": s.arrival_accepted_at,
                    "arrival_amount": str(s.arrival_amount.quantize(Q)),
                    "account_no": s.account_no,
                    "allocation_detail_url": s.allocation_detail_url,
                    "arrival_detail_url": s.arrival_detail_url,
                }
                for s in self.sources
            ],
            "excluded": [
                {
                    "record_id": e.record_id,
                    "kind": e.kind,
                    "amount": money(e.amount),
                    "reason": e.reason,
                    "detail_url": e.detail_url,
                }
                for e in self.excluded
            ],
            "arrival_count": self.arrival_count,
            "reversal_count": self.reversal_count,
            "allocation_count": self.allocation_count,
        }


def _parse_ts(raw: str, where: str) -> datetime:
    text = raw.strip()
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise BondVerifyError(f"{where} 受理时间无法解析: {raw!r}") from exc


def _as_decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise BondVerifyError(f"{where} 金额无法解析: {value!r}") from exc


def _indeterminate(reasons: list[str], records: list[dict[str, Any]],
                   required: Decimal) -> VerificationResult:
    """结构校验失败：只做计数，不输出任何金额结论。"""
    counts = {"arrival": 0, "reversal": 0, "allocation": 0}
    for r in records:
        kind = r.get("kind")
        if kind in counts:
            counts[kind] += 1
    return VerificationResult(
        status="indeterminate",
        reasons=sorted(set(reasons)),
        required_amount=required,
        arrival_count=counts["arrival"],
        reversal_count=counts["reversal"],
        allocation_count=counts["allocation"],
    )


def verify_bond(
    *,
    announcement_code: str,
    lot: str,
    enterprise_id: str,
    required_amount: Decimal | str,
    account_no: str,
    deadline_text: str,
    records: list[dict[str, Any]],
) -> VerificationResult:
    """对单个目标标段执行保证金核验。records 为平台全量明细（不限企业）。"""
    required = _as_decimal(required_amount, "目标标段应缴金额").quantize(Q)
    account_no = account_no.strip()
    target_lot_ref = f"{announcement_code.strip()}/{lot.strip()}"
    deadline = _parse_ts(deadline_text, "投标截止时间")

    # 统一金额为 Decimal，并预先解析全部受理时间
    norm: list[dict[str, Any]] = []
    structural_errors: list[str] = []
    for r in records:
        row = dict(r)
        row["amount_dec"] = _as_decimal(row.get("amount"), f"明细 {row.get('record_id')}")
        try:
            row["accepted_dt"] = _parse_ts(
                str(row.get("accepted_at", "")), f"明细 {row.get('record_id')}"
            )
        except BondVerifyError as exc:
            structural_errors.append(str(exc))
            row["accepted_dt"] = None
        norm.append(row)
    # 时间解析失败后后续时间比较不再安全，直接判定无法判定
    if structural_errors:
        return _indeterminate(structural_errors, norm, required)

    # --- 结构校验 1：同编号冲突 ------------------------------------------------
    by_id: dict[str, dict[str, Any]] = {}
    for row in norm:
        rid = row.get("record_id")
        if not rid:
            structural_errors.append("存在缺少 record_id 的明细")
            continue
        if rid in by_id:
            prev = by_id[rid]
            if _fingerprint(prev) != _fingerprint(row):
                structural_errors.append(f"同编号明细冲突: {rid} 的内容不一致")
            else:
                structural_errors.append(f"同编号明细重复: {rid} 出现多条记录")
        else:
            by_id[rid] = row

    arrivals = {rid: row for rid, row in by_id.items() if row.get("kind") == "arrival"}

    # --- 结构校验 2：引用悬空 + 企业归属 --------------------------------------
    def resolve(rid: str | None, owner: dict[str, Any], ref_field: str) -> dict[str, Any] | None:
        if not rid:
            return None
        target = by_id.get(rid)
        if target is None or target.get("kind") != "arrival":
            structural_errors.append(
                f"引用悬空: 明细 {owner.get('record_id')} 的 {ref_field} 指向不存在的到账 {rid}"
            )
            return None
        if target.get("enterprise_id") != owner.get("enterprise_id"):
            structural_errors.append(
                f"企业归属冲突: 明细 {owner.get('record_id')} 引用了其他企业的到账 {rid}"
            )
        return target

    for row in norm:
        if row.get("kind") == "allocation":
            resolve(row.get("arrival_id"), row, "arrival_id")
        elif row.get("kind") == "reversal":
            resolve(row.get("reverses_id"), row, "reverses_id")

    # --- 结构校验 3：分配超额（按到账汇总全部分配，跨标段/跨页一致计算） ------
    allocated_total: dict[str, Decimal] = {}
    for row in norm:
        if row.get("kind") == "allocation" and row.get("arrival_id") in arrivals:
            allocated_total[row["arrival_id"]] = (
                allocated_total.get(row["arrival_id"], ZERO) + row["amount_dec"]
            )
    for arrival_id, total in allocated_total.items():
        if total > arrivals[arrival_id]["amount_dec"]:
            structural_errors.append(
                "分配超额: 到账 "
                f"{arrival_id} 实收 {arrivals[arrival_id]['amount_dec']}，"
                f"各标段分配合计 {total}"
            )

    if structural_errors:
        return _indeterminate(structural_errors, norm, required)

    # --- 资金还原 -------------------------------------------------------------
    ent_rows = [r for r in norm if r.get("enterprise_id") == enterprise_id]
    ent_arrivals = {r["record_id"]: r for r in ent_rows if r["kind"] == "arrival"}
    ent_reversals = [r for r in ent_rows if r["kind"] == "reversal"]
    ent_allocations = [r for r in ent_rows if r["kind"] == "allocation"]

    # 截止前冲正：冲正受理时间 <= 截止时间，对应到账整体失效
    reversed_before_deadline: dict[str, list[str]] = {}
    excluded: list[ExcludedItem] = []
    for rev in ent_reversals:
        arrival_id = rev["reverses_id"]
        if rev["accepted_dt"] <= deadline:
            reversed_before_deadline.setdefault(arrival_id, []).append(rev["record_id"])
            excluded.append(
                ExcludedItem(
                    record_id=rev["record_id"],
                    kind="reversal",
                    amount=rev["amount_dec"],
                    reason=(
                        f"截止前冲正到账 {arrival_id}，该到账及其分配不计入"
                        f"（受理时间 {rev['accepted_at']} ≤ 截止 {deadline_text}）"
                    ),
                    detail_url=rev.get("detail_url", ""),
                )
            )
        else:
            excluded.append(
                ExcludedItem(
                    record_id=rev["record_id"],
                    kind="reversal",
                    amount=rev["amount_dec"],
                    reason=(
                        f"截止后冲正到账 {arrival_id}，不影响截止前已形成的资金"
                        f"（受理时间 {rev['accepted_at']} 晚于截止 {deadline_text}）"
                    ),
                    detail_url=rev.get("detail_url", ""),
                )
            )

    def arrival_state(arrival: dict[str, Any]) -> tuple[bool, str | None]:
        """返回 (是否有效, 无效原因)。"""
        if arrival["accepted_dt"] > deadline:
            return False, (
                f"到账受理时间 {arrival['accepted_at']} 晚于截止时间 {deadline_text}"
            )
        if (arrival.get("account_no") or "").strip() != account_no:
            return False, (
                f"汇入账户 {arrival.get('account_no')} 非公告指定账户 {account_no}，不计入"
            )
        if arrival["record_id"] in reversed_before_deadline:
            rev_ids = ",".join(reversed_before_deadline[arrival["record_id"]])
            return False, f"到账已于截止前被冲正（冲正明细 {rev_ids}）"
        return True, None

    arrival_ok: dict[str, bool] = {}
    for arrival in ent_arrivals.values():
        ok, reason = arrival_state(arrival)
        arrival_ok[arrival["record_id"]] = ok
        if not ok:
            excluded.append(
                ExcludedItem(
                    record_id=arrival["record_id"],
                    kind="arrival",
                    amount=arrival["amount_dec"],
                    reason=reason or "",
                    detail_url=arrival.get("detail_url", ""),
                )
            )

    sources: list[FundSource] = []
    valid_amount = ZERO
    for alloc in ent_allocations:
        arrival = ent_arrivals.get(alloc["arrival_id"])
        # 引用悬空已在结构校验拦截，这里 arrival 必然存在
        assert arrival is not None
        if alloc["accepted_dt"] > deadline:
            excluded.append(
                ExcludedItem(
                    record_id=alloc["record_id"],
                    kind="allocation",
                    amount=alloc["amount_dec"],
                    reason=(
                        f"分配受理时间 {alloc['accepted_at']} 晚于截止时间 {deadline_text}"
                    ),
                    detail_url=alloc.get("detail_url", ""),
                )
            )
            continue
        if not arrival_ok[arrival["record_id"]]:
            _, arr_reason = arrival_state(arrival)
            excluded.append(
                ExcludedItem(
                    record_id=alloc["record_id"],
                    kind="allocation",
                    amount=alloc["amount_dec"],
                    reason=f"引用的到账 {arrival['record_id']} 不计入：{arr_reason}",
                    detail_url=alloc.get("detail_url", ""),
                )
            )
            continue
        if (alloc.get("lot") or "").strip() != target_lot_ref:
            excluded.append(
                ExcludedItem(
                    record_id=alloc["record_id"],
                    kind="allocation",
                    amount=alloc["amount_dec"],
                    reason=f"分配至其他标段 {alloc.get('lot')}，不计入目标标段 {target_lot_ref}",
                    detail_url=alloc.get("detail_url", ""),
                )
            )
            continue
        sources.append(
            FundSource(
                allocation_id=alloc["record_id"],
                arrival_id=arrival["record_id"],
                allocation_accepted_at=alloc["accepted_at"],
                amount=alloc["amount_dec"],
                arrival_accepted_at=arrival["accepted_at"],
                arrival_amount=arrival["amount_dec"],
                account_no=arrival.get("account_no", ""),
                allocation_detail_url=alloc.get("detail_url", ""),
                arrival_detail_url=arrival.get("detail_url", ""),
            )
        )
        valid_amount += alloc["amount_dec"]

    sources.sort(key=lambda s: (s.allocation_accepted_at, s.allocation_id))
    excluded.sort(key=lambda e: (e.record_id, e.kind))
    valid_amount = valid_amount.quantize(Q)
    gap = max(ZERO, required - valid_amount)

    return VerificationResult(
        status="verified",
        required_amount=required,
        valid_amount=valid_amount,
        gap=gap,
        is_sufficient=valid_amount >= required,
        sources=sources,
        excluded=excluded,
        arrival_count=sum(1 for r in ent_rows if r["kind"] == "arrival"),
        reversal_count=len(ent_reversals),
        allocation_count=len(ent_allocations),
    )


def _fingerprint(row: dict[str, Any]) -> tuple:
    return tuple(
        str(row.get(k, ""))
        for k in (
            "record_id", "kind", "enterprise_id", "accepted_at", "amount",
            "account_no", "reverses_id", "arrival_id", "lot",
        )
    )
