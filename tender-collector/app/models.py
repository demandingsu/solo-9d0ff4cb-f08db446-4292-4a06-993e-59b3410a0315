from __future__ import annotations

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


class BondVerifyRequest(BaseModel):
    announcement_code: str = Field(
        ..., min_length=1, description="已采集的原公告编号，例如 SZ-2026-001"
    )
    lot: str = Field(..., min_length=1, description="标段号，例如 G1")
    enterprise_id: str = Field(..., min_length=1, description="投标企业编号")
    catalog_url: str = Field(
        ..., min_length=1,
        description="保证金平台明细目录 URL（服务沿同站分页读取全部明细）",
    )


class FundSourceOut(BaseModel):
    allocation_id: str
    arrival_id: str
    allocation_accepted_at: str
    amount: str
    arrival_accepted_at: str
    arrival_amount: str
    account_no: str
    allocation_detail_url: str
    arrival_detail_url: str


class ExcludedItemOut(BaseModel):
    record_id: str
    kind: str
    amount: str | None
    reason: str
    detail_url: str


class BondVerifyResponse(BaseModel):
    # verified=账目自洽并完成核验；indeterminate=残缺/冲突，无法判定（不判不足）
    status: str
    reasons: list[str]
    announcement_code: str
    lot: str
    enterprise_id: str
    target_lot: str
    deadline: str | None
    deadline_source_code: str | None
    deadline_source_url: str | None
    required_amount: str | None
    account_no: str | None
    valid_amount: str | None
    gap: str | None
    is_sufficient: bool | None
    sources: list[FundSourceOut]
    excluded: list[ExcludedItemOut]
    arrival_count: int
    reversal_count: int
    allocation_count: int
    catalog_url: str
    announcement_detail_url: str | None
