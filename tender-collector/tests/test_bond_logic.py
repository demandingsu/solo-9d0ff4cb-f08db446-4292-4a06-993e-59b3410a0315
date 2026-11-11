"""app/bond.py 纯逻辑单元测试：不启动 HTTP 服务，直接构造明细字典。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.bond import BondVerifyError, verify_bond

DEADLINE = "2026-11-20 15:00"
ACCOUNT = "ACC-1"
OTHER_ACCOUNT = "ACC-2"


def _arrival(rid, ts, amount="100.00", account=ACCOUNT, ent="E1", **kw):
    return {
        "record_id": rid, "kind": "arrival", "enterprise_id": ent,
        "enterprise_name": "测试企业", "accepted_at": ts, "amount": amount,
        "account_no": account, "reverses_id": None, "arrival_id": None,
        "lot": None, "detail_url": f"http://x/{rid}", **kw,
    }


def _reversal(rid, ts, reverses, amount="100.00", ent="E1"):
    return {
        "record_id": rid, "kind": "reversal", "enterprise_id": ent,
        "enterprise_name": "测试企业", "accepted_at": ts, "amount": amount,
        "account_no": ACCOUNT, "reverses_id": reverses, "arrival_id": None,
        "lot": None, "detail_url": f"http://x/{rid}",
    }


def _allocation(rid, ts, arrival, lot, amount="100.00", ent="E1"):
    return {
        "record_id": rid, "kind": "allocation", "enterprise_id": ent,
        "enterprise_name": "测试企业", "accepted_at": ts, "amount": amount,
        "account_no": None, "reverses_id": None, "arrival_id": arrival,
        "lot": lot, "detail_url": f"http://x/{rid}",
    }


def _call(records, lot="G1", ent="E1", required="100.00", deadline=DEADLINE,
          account=ACCOUNT):
    return verify_bond(
        announcement_code="SZ-1", lot=lot, enterprise_id=ent,
        required_amount=required, account_no=account,
        deadline_text=deadline, records=records,
    )


def test_basic_sufficient_and_source_traceability():
    records = [
        _arrival("A1", "2026-11-18 09:00"),
        _allocation("L1", "2026-11-18 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("100.00")
    assert r.gap == Decimal("0.00")
    assert r.is_sufficient is True
    assert len(r.sources) == 1
    src = r.sources[0]
    assert src.allocation_id == "L1" and src.arrival_id == "A1"
    assert src.account_no == ACCOUNT


def test_insufficient_gap_and_no_false_indeterminate():
    records = [
        _arrival("A1", "2026-11-18 09:00", amount="60.00"),
        _allocation("L1", "2026-11-18 10:00", "A1", "SZ-1/G1", amount="60.00"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("60.00")
    assert r.gap == Decimal("40.00")
    assert r.is_sufficient is False


def test_boundary_arrival_exactly_at_deadline_counts():
    records = [
        _arrival("A1", DEADLINE),
        _allocation("L1", DEADLINE, "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("100.00")


def test_arrival_one_minute_after_deadline_excluded():
    records = [
        _arrival("A1", "2026-11-20 15:01"),
        _allocation("L1", "2026-11-20 15:01", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("0.00")
    assert r.gap == Decimal("100.00")
    kinds = {e.record_id: e.kind for e in r.excluded}
    assert kinds == {"A1": "arrival", "L1": "allocation"}


def test_reversal_before_deadline_removes_arrival_and_allocations():
    records = [
        _arrival("A1", "2026-11-10 09:00"),
        _reversal("R1", "2026-11-11 09:00", "A1"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("0.00")
    excluded = {e.record_id for e in r.excluded}
    assert {"A1", "R1", "L1"} <= excluded


def test_reversal_after_deadline_does_not_void_funds():
    records = [
        _arrival("A1", "2026-11-10 09:00"),
        _reversal("R1", "2026-11-21 09:00", "A1"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("100.00")
    assert len(r.sources) == 1
    assert any("截止后冲正" in e.reason and e.record_id == "R1" for e in r.excluded)


def test_wrong_account_excluded_even_if_allocated_to_target():
    records = [
        _arrival("A1", "2026-11-10 09:00", account=OTHER_ACCOUNT),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("0.00")
    assert any("非公告指定账户" in e.reason for e in r.excluded)


def test_cross_lot_allocation_constrained_by_arrival_amount():
    # 到账 100，分给 G1 80 + G2 20（合计 100）：合法
    records = [
        _arrival("A1", "2026-11-10 09:00"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", amount="80.00"),
        _allocation("L2", "2026-11-10 10:01", "A1", "SZ-1/G2", amount="20.00"),
    ]
    r = _call(records, required="80.00")
    assert r.status == "verified"
    assert r.valid_amount == Decimal("80.00")
    assert any(e.record_id == "L2" and "其他标段" in e.reason for e in r.excluded)

    # 合计 130 > 实收 100：分配超额 -> 无法判定
    records[2] = _allocation("L2", "2026-11-10 10:01", "A1", "SZ-1/G2", amount="50.00")
    r = _call(records, required="80.00")
    assert r.status == "indeterminate"
    assert r.is_sufficient is None and r.valid_amount is None
    assert any("分配超额" in x for x in r.reasons)


def test_overallocation_spotted_even_when_other_enterprise_owns_it():
    # 超额发生在别的企业账上，目标企业的核验仍必须无法判定（账目整体不可信）
    records = [
        _arrival("A1", "2026-11-10 09:00", ent="E2"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", amount="60.00", ent="E2"),
        _allocation("L2", "2026-11-10 10:01", "A1", "SZ-1/G2", amount="60.00", ent="E2"),
        _arrival("A9", "2026-11-12 09:00"),
        _allocation("L9", "2026-11-12 10:00", "A9", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"
    assert any("分配超额" in x for x in r.reasons)


def test_dangling_allocation_reference_is_indeterminate():
    records = [
        _arrival("A1", "2026-11-10 09:00"),
        _allocation("L1", "2026-11-10 10:00", "A-MISSING", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"
    assert any("引用悬空" in x for x in r.reasons)


def test_reversal_dangling_is_indeterminate():
    records = [
        _arrival("A1", "2026-11-10 09:00"),
        _reversal("R1", "2026-11-11 09:00", "A-MISSING"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"
    assert any("引用悬空" in x for x in r.reasons)


def test_allocation_referencing_other_enterprise_arrival_is_indeterminate():
    records = [
        _arrival("A1", "2026-11-10 09:00", ent="E2"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", ent="E1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"
    assert any("企业归属冲突" in x for x in r.reasons)


def test_duplicate_record_id_conflict_is_indeterminate():
    records = [
        _arrival("A1", "2026-11-10 09:00", amount="100.00"),
        _arrival("A1", "2026-11-10 09:00", amount="120.00"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"
    assert any("同编号明细冲突" in x for x in r.reasons)


def test_duplicate_record_id_identical_content_still_indeterminate():
    records = [
        _arrival("A1", "2026-11-10 09:00"),
        _arrival("A1", "2026-11-10 10:00"),  # 同编号即重复（受理时间不同）
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"


def test_unparseable_accepted_at_is_indeterminate():
    records = [
        _arrival("A1", "not-a-date"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "indeterminate"
    assert any("受理时间无法解析" in x for x in r.reasons)


def test_unparseable_deadline_raises():
    records = [_arrival("A1", "2026-11-10 09:00")]
    with pytest.raises(BondVerifyError):
        _call(records, deadline="bad-deadline")


def test_order_independence():
    """打乱明细顺序，核验结果必须一致。"""
    import random

    records = [
        _arrival("A1", "2026-11-10 09:00", amount="60.00"),
        _arrival("A2", "2026-11-12 09:00", amount="40.00"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", amount="60.00"),
        _allocation("L2", "2026-11-12 10:00", "A2", "SZ-1/G1", amount="40.00"),
        _allocation("L3", "2026-11-12 10:01", "A2", "SZ-1/G2", amount="0.00"),
    ]
    baseline = _call(records).as_dict()
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    assert _call(shuffled).as_dict() == baseline


def test_unknown_enterprise_is_verified_zero_not_indeterminate():
    """企业没有任何明细是业务事实（零到账），不是账目残缺。"""
    records = [
        _arrival("A1", "2026-11-10 09:00", ent="E2"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", ent="E2"),
    ]
    r = _call(records, ent="E-NOBODY")
    assert r.status == "verified"
    assert r.valid_amount == Decimal("0.00")
    assert r.is_sufficient is False
    assert r.arrival_count == 0


def test_allocation_before_deadline_but_arrival_after_deadline_excluded():
    records = [
        _arrival("A1", "2026-11-21 09:00"),
        # 分配时间戳早于到账在真实系统中不该出现；规则上到账无效则分配必无效
        _allocation("L1", "2026-11-19 10:00", "A1", "SZ-1/G1"),
    ]
    r = _call(records)
    assert r.status == "verified"
    assert r.valid_amount == Decimal("0.00")
    assert any(e.record_id == "L1" and "A1" in e.reason for e in r.excluded)


def test_amounts_quantized_to_two_decimals():
    records = [
        _arrival("A1", "2026-11-10 09:00", amount="99.999"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", amount="99.999"),
    ]
    r = _call(records, required="100.00")
    assert r.status == "verified"
    d = r.as_dict()
    assert d["valid_amount"] == "100.00"
    assert d["gap"] == "0.00"


def test_catalog_counts_reflect_target_enterprise_only():
    records = [
        _arrival("A1", "2026-11-10 09:00", ent="E1"),
        _arrival("A2", "2026-11-10 09:00", ent="E2"),
        _allocation("L1", "2026-11-10 10:00", "A1", "SZ-1/G1", ent="E1"),
        _allocation("L2", "2026-11-10 10:00", "A2", "SZ-1/G1", ent="E2"),
        _reversal("R1", "2026-11-11 10:00", "A1", ent="E1"),
    ]
    r = _call(records)
    assert r.arrival_count == 1
    assert r.allocation_count == 1
    assert r.reversal_count == 1
