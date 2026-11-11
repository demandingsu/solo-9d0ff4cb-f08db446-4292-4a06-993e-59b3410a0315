from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .bond_service import BondIndeterminate, BondInputError, verify
from .models import (
    BondVerifyRequest,
    BondVerifyResponse,
    CollectRequest,
    CollectResponse,
    ProjectOut,
    RunOut,
)
from .service import ServiceError, query_projects, run_collection


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集与保证金核验 API",
    description=(
        "提交公告列表页地址，服务抓取全部详情页，将更正公告按引用的原公告编号归并，"
        "按发布时间确定生效的投标截止时间。重复采集不产生重复记录；"
        "任一详情页读取失败时整体报错并保留上次成功采集结果。"
        "另提供 /api/bond/verify 保证金核验：以生效截止时间为界，按受理时间还原"
        "企业到账、冲正与标段分配，账目残缺或冲突时标为无法判定，不把残缺账目判成不足。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]


@app.post("/api/bond/verify", response_model=BondVerifyResponse, status_code=200)
def verify_bond_endpoint(req: BondVerifyRequest) -> BondVerifyResponse:
    """核验目标标段保证金是否在截标前足额到账。

    账目残缺/冲突/页面不可读时不判“不足”，而是返回 status=indeterminate；
    只有请求引用的原公告或标段在本地采集数据中不存在时才报 404。
    """
    try:
        payload = verify(
            announcement_code=req.announcement_code,
            lot=req.lot,
            enterprise_id=req.enterprise_id,
            catalog_url=req.catalog_url,
        )
    except BondInputError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BondIndeterminate as exc:
        return BondVerifyResponse(
            status="indeterminate",
            reasons=exc.reasons,
            announcement_code=req.announcement_code,
            lot=req.lot,
            enterprise_id=req.enterprise_id,
            target_lot=f"{req.announcement_code}/{req.lot}",
            deadline=None,
            deadline_source_code=None,
            deadline_source_url=None,
            required_amount=None,
            account_no=None,
            valid_amount=None,
            gap=None,
            is_sufficient=None,
            sources=[],
            excluded=[],
            arrival_count=0,
            reversal_count=0,
            allocation_count=0,
            catalog_url=req.catalog_url,
            announcement_detail_url=None,
        )
    return BondVerifyResponse(**payload)
