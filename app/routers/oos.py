"""OOS Investigation router (Pharma IMS Module 3). Mounts at /api/oos.

Two-phase OOS protocol. Phase 1, Phase 2, and batch disposition each require a
21 CFR Part 11 electronic signature. A Phase 2 manufacturing cause spawns a
Deviation. RBAC: reads OOS.READ; create OOS.CREATE; phases OOS.UPDATE;
disposition OOS.APPROVE.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.oos import OosInvestigation
from app.models.user import User
from app.services import oos as svc
from app.services import part11
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/oos", tags=["oos"])

PHASE1_CONCLUSIONS = [
    "laboratory_error_identified_result_invalidated",
    "no_laboratory_error_proceeds_to_phase_2",
    "retest_confirms_oos",
    "retest_within_specification_query_raised",
]
PHASE2_CONCLUSIONS = ["manufacturing_cause_identified", "no_cause_identified_batch_rejected", "retest_results_within_specification"]
DISPOSITIONS = ["release", "reject", "reprocess", "retest", "quarantine_pending", "destroyed"]
LAB_CHECKS = ["calculation_review", "instrument_calibration_check", "standard_review", "reagent_check",
              "sample_integrity", "glassware_check", "environmental_conditions", "analyst_training_verification",
              "system_suitability_review"]


async def _require(db: AsyncSession, user: User, code: str) -> None:
    r = await can(db, user.id, code, PermissionContext())
    if not r.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, r.reason or f"Missing permission {code}")


async def _load(db: AsyncSession, oid: str) -> OosInvestigation:
    o = await db.get(OosInvestigation, oid)
    if o is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OOS not found")
    return o


def _ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


class CreateBody(BaseModel):
    plantId: str
    productName: str = Field(min_length=2)
    batchNumber: str = Field(min_length=1)
    testName: str = Field(min_length=2)
    specificationReference: str = ""
    specificationLimit: str = ""
    initialResult: str
    initialResultNumeric: float | None = None
    resultUnit: str = ""
    analysisDate: datetime | None = None
    instrumentId: str | None = None


class Phase1Body(BaseModel):
    password: str
    conclusion: str
    assignableCauseFound: bool = False
    assignableCauseDescription: str = ""
    resultInvalidated: bool = False
    retestAuthorized: bool = False
    retestResults: list[dict] = []
    checks: list[dict] = []


class Phase2Body(BaseModel):
    password: str
    rootCauseCategory: str
    rootCauseDescription: str = Field(min_length=5)
    conclusion: str
    spawnDeviation: bool = True
    deviationSeverity: str = "major"


class DispositionBody(BaseModel):
    password: str
    disposition: str
    justification: str = ""


@router.get("/reference")
async def reference(user: User = Depends(get_current_user)) -> dict:
    return {"phase1Conclusions": PHASE1_CONCLUSIONS, "phase2Conclusions": PHASE2_CONCLUSIONS,
            "dispositions": DISPOSITIONS, "labChecks": LAB_CHECKS}


@router.get("/dashboard")
async def dashboard(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.READ")
    return await svc.dashboard(db, plant_id=plantId)


@router.get("")
async def list_oos(plantId: str = Query(...), status_filter: str | None = Query(None, alias="status"),
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.READ")
    stmt = select(OosInvestigation).where(OosInvestigation.plantId == plantId)
    if status_filter:
        stmt = stmt.where(OosInvestigation.status == status_filter)
    rows = (await db.execute(stmt.order_by(OosInvestigation.createdAt.desc()))).scalars().all()
    return {"plantId": plantId, "count": len(rows), "oos": [svc.to_dict(o) for o in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: CreateBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.CREATE")
    o = await svc.create_oos(db, user=user, data=body.model_dump())
    await db.commit()
    return svc.to_dict(o)


@router.get("/{oid}")
async def get_oos(oid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.READ")
    o = await _load(db, oid)
    analyst = await db.get(User, o.analystUserId)
    return {
        "oos": svc.to_dict(o),
        "analystName": analyst.name if analyst else None,
        "signatures": await part11.signatures_for(db, "oos", o.id, current_snapshot=svc.oos_snapshot(o)),
        "auditTrail": await part11.audit_for(db, "oos", o.id),
    }


@router.post("/{oid}/phase1")
async def phase1(oid: str, body: Phase1Body, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.UPDATE")
    o = await _load(db, oid)
    try:
        await svc.record_phase1(db, o=o, user=user, password=body.password, ip=_ip(request),
                                checks=body.checks, assignable_cause_found=body.assignableCauseFound,
                                assignable_cause_description=body.assignableCauseDescription,
                                result_invalidated=body.resultInvalidated, retest_authorized=body.retestAuthorized,
                                retest_results=body.retestResults, conclusion=body.conclusion)
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(o)


@router.post("/{oid}/phase2")
async def phase2(oid: str, body: Phase2Body, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.UPDATE")
    o = await _load(db, oid)
    try:
        await svc.record_phase2(db, o=o, user=user, password=body.password, ip=_ip(request),
                                root_cause_category=body.rootCauseCategory, root_cause_description=body.rootCauseDescription,
                                conclusion=body.conclusion, spawn_deviation=body.spawnDeviation, deviation_severity=body.deviationSeverity)
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(o)


@router.post("/{oid}/disposition")
async def disposition(oid: str, body: DispositionBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "OOS.APPROVE")
    o = await _load(db, oid)
    try:
        await svc.record_disposition(db, o=o, user=user, password=body.password, ip=_ip(request),
                                     disposition=body.disposition, justification=body.justification)
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(o)
