"""Audit Management router (Pharma IMS Module 4). Mounts at /api/audits.

Audit lifecycle + findings → CAPA. Report issuance and audit closure are
21 CFR Part 11 e-signed. RBAC: reads AUDIT.READ; create AUDIT.CREATE; execution
AUDIT.UPDATE; closure AUDIT.CLOSE.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.audit_mgmt import Audit, AuditFinding
from app.models.user import User
from app.services import audit_mgmt as svc
from app.services import part11
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/audits", tags=["audits"])

AUDIT_TYPES = [
    {"code": "internal_gmp", "name": "Internal GMP (Self-Inspection)"},
    {"code": "internal_hse", "name": "Internal HSE"},
    {"code": "internal_integrated", "name": "Internal Integrated (GMP + HSE)"},
    {"code": "supplier_audit", "name": "Supplier Audit"},
    {"code": "regulatory_inspection", "name": "Regulatory Inspection"},
    {"code": "customer_audit", "name": "Customer Audit"},
    {"code": "certification_audit", "name": "Certification Audit"},
    {"code": "mock_inspection", "name": "Mock Inspection"},
]
FINDING_TYPES = ["critical", "major", "minor", "observation", "opportunity_for_improvement"]


async def _require(db: AsyncSession, user: User, code: str) -> None:
    r = await can(db, user.id, code, PermissionContext())
    if not r.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, r.reason or f"Missing permission {code}")


async def _load(db: AsyncSession, aid: str) -> Audit:
    a = (await db.execute(select(Audit).where(Audit.id == aid).options(selectinload(Audit.findings)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit not found")
    return a


async def _finding(db: AsyncSession, fid: str) -> AuditFinding:
    f = await db.get(AuditFinding, fid)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Finding not found")
    return f


def _ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


class CreateBody(BaseModel):
    plantId: str
    title: str = Field(min_length=4)
    auditType: str
    description: str = ""
    scope: list[str] = []
    applicableStandards: list[str] = []
    regulatoryAuthority: str | None = None
    supplierName: str | None = None
    plannedStart: datetime | None = None
    plannedEnd: datetime | None = None
    leadAuditorUserId: str | None = None
    auditTeam: list[str] = []
    auditeeDepartmentHeadUserId: str | None = None


class FindingBody(BaseModel):
    type: str
    description: str = Field(min_length=4)
    area: str = ""
    referenceRequirement: str = ""
    evidence: str = ""


class RespondBody(BaseModel):
    response: str = Field(min_length=2)


class RaiseCapaBody(BaseModel):
    ownerUserId: str | None = None


class PwBody(BaseModel):
    password: str


# ─── Static ──────────────────────────────────────────────────────────────


@router.get("/reference")
async def reference(user: User = Depends(get_current_user)) -> dict:
    return {"auditTypes": AUDIT_TYPES, "findingTypes": FINDING_TYPES}


@router.get("/dashboard")
async def dashboard(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.READ")
    return await svc.dashboard(db, plant_id=plantId)


@router.get("/users")
async def users(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.READ")
    rows = (await db.execute(select(User).where(User.plantId == plantId).order_by(User.name.asc()))).scalars().all()
    return {"users": [{"id": u.id, "name": u.name, "role": u.role, "department": u.department or ""} for u in rows]}


@router.get("")
async def list_audits(plantId: str = Query(...), status_filter: str | None = Query(None, alias="status"),
                      user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.READ")
    stmt = select(Audit).where(Audit.plantId == plantId).options(selectinload(Audit.findings))
    if status_filter:
        stmt = stmt.where(Audit.status == status_filter)
    rows = (await db.execute(stmt.order_by(Audit.createdAt.desc()))).scalars().all()
    return {"plantId": plantId, "count": len(rows), "audits": [svc.to_dict(a, a.findings) for a in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: CreateBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.CREATE")
    a = await svc.create_audit(db, user=user, data=body.model_dump())
    await db.commit()
    return svc.to_dict(a, [])


# ─── Detail + actions ────────────────────────────────────────────────────


@router.get("/{aid}")
async def get_audit(aid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.READ")
    a = await _load(db, aid)
    findings = sorted(a.findings, key=lambda f: f.findingNumber)
    lead = await db.get(User, a.leadAuditorUserId)
    return {
        "audit": svc.to_dict(a, findings),
        "leadAuditorName": lead.name if lead else None,
        "findings": [svc.finding_dict(f) for f in findings],
        "signatures": await part11.signatures_for(db, "audit", a.id, current_snapshot=svc.audit_snapshot(a)),
        "auditTrail": await part11.audit_for(db, "audit", a.id),
    }


@router.post("/{aid}/start")
async def start(aid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.UPDATE")
    a = await _load(db, aid)
    await svc.start_audit(db, a=a, user=user)
    await db.commit()
    return svc.to_dict(a, a.findings)


@router.post("/{aid}/findings", status_code=status.HTTP_201_CREATED)
async def add_finding(aid: str, body: FindingBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.UPDATE")
    a = await _load(db, aid)
    f = await svc.add_finding(db, a=a, user=user, type_=body.type, description=body.description,
                              area=body.area, reference_requirement=body.referenceRequirement, evidence=body.evidence)
    await db.commit()
    return svc.finding_dict(f)


@router.post("/{aid}/issue-report")
async def issue_report(aid: str, body: PwBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.UPDATE")
    a = await _load(db, aid)
    try:
        await svc.issue_report(db, a=a, user=user, password=body.password, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(a, a.findings)


@router.post("/{aid}/findings/{fid}/respond")
async def respond(aid: str, fid: str, body: RespondBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.UPDATE")
    a = await _load(db, aid)
    f = await _finding(db, fid)
    await svc.respond_finding(db, finding=f, audit=a, user=user, response=body.response)
    await db.commit()
    return svc.finding_dict(f)


@router.post("/{aid}/findings/{fid}/raise-capa")
async def raise_capa(aid: str, fid: str, body: RaiseCapaBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.UPDATE")
    a = await _load(db, aid)
    f = await _finding(db, fid)
    try:
        result = await svc.raise_capa_for_finding(db, audit=a, finding=f, user=user, owner_user_id=body.ownerUserId)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not raise CAPA: {e}")
    await db.commit()
    return {**svc.finding_dict(f), "raisedCapa": result}


@router.post("/{aid}/findings/{fid}/close")
async def close_finding(aid: str, fid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.UPDATE")
    a = await _load(db, aid)
    f = await _finding(db, fid)
    await svc.close_finding(db, finding=f, audit=a, user=user)
    await db.commit()
    return svc.finding_dict(f)


@router.post("/{aid}/close")
async def close_audit(aid: str, body: PwBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "AUDIT.CLOSE")
    a = await _load(db, aid)
    try:
        await svc.close_audit(db, a=a, user=user, password=body.password, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(a, a.findings)
