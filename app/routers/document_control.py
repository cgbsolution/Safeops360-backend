"""Document Control router (Pharma IMS Module 2). Mounts at /api/documents.

Controlled-document lifecycle + search/retrieval. Technical/QA review, approval,
and read-acknowledgment each require a 21 CFR Part 11 electronic signature.
RBAC: reads DOCUMENT.READ; author actions DOCUMENT.CREATE; reviews
DOCUMENT.REVIEW; approval/effective/obsolete DOCUMENT.APPROVE.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.document_control import ControlledDocument, DocumentVersion
from app.models.user import User
from app.services import document_control as svc
from app.services import part11
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/documents", tags=["documents"])

DOC_TYPES = [
    {"code": "sop", "name": "Standard Operating Procedure", "prefix": "SOP", "reviewMonths": 24},
    {"code": "work_instruction", "name": "Work Instruction", "prefix": "WI", "reviewMonths": 24},
    {"code": "specification", "name": "Specification", "prefix": "SPEC", "reviewMonths": 24},
    {"code": "batch_manufacturing_record", "name": "Batch Manufacturing Record", "prefix": "BMR", "reviewMonths": 24},
    {"code": "method", "name": "Analytical Method", "prefix": "MTH", "reviewMonths": 24},
    {"code": "cleaning_procedure", "name": "Cleaning Procedure", "prefix": "CLN", "reviewMonths": 24},
    {"code": "validation_protocol", "name": "Validation Protocol", "prefix": "VAL", "reviewMonths": 36},
    {"code": "form", "name": "Form / Logbook", "prefix": "FORM", "reviewMonths": 24},
    {"code": "policy", "name": "Policy", "prefix": "POL", "reviewMonths": 24},
]


async def _require(db: AsyncSession, user: User, code: str) -> None:
    r = await can(db, user.id, code, PermissionContext())
    if not r.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, r.reason or f"Missing permission {code}")


async def _load(db: AsyncSession, doc_id: str) -> ControlledDocument:
    doc = await db.get(ControlledDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


def _ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


class CreateBody(BaseModel):
    plantId: str
    title: str = Field(min_length=4)
    documentType: str
    category: str = ""
    documentNumber: str | None = None
    changeSummary: str = "Initial issue."
    documentFileHash: str | None = None
    distributeToUserIds: list[str] = []
    applicableAreas: list[str] = []
    applicableProducts: list[str] = []
    reviewFrequencyMonths: int = 24
    regulatoryReference: str = ""
    requiresTrainingOnNewVersion: bool = False


class PwBody(BaseModel):
    password: str


class ApproveBody(BaseModel):
    password: str
    effectiveFrom: datetime | None = None


class ReviseBody(BaseModel):
    changeSummary: str = Field(min_length=4)
    documentFileHash: str | None = None


class ObsoleteBody(BaseModel):
    reason: str = ""


# ─── Static routes ───────────────────────────────────────────────────────


@router.get("/types")
async def types(user: User = Depends(get_current_user)) -> dict:
    return {"types": DOC_TYPES}


@router.get("/dashboard")
async def dashboard(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.READ")
    return await svc.dashboard(db, plant_id=plantId)


@router.get("")
async def list_documents(
    plantId: str = Query(...),
    documentType: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DOCUMENT.READ")
    stmt = select(ControlledDocument).where(ControlledDocument.plantId == plantId)
    if documentType:
        stmt = stmt.where(ControlledDocument.documentType == documentType)
    if status_filter:
        stmt = stmt.where(ControlledDocument.currentVersionStatus == status_filter)
    rows = (await db.execute(stmt.order_by(ControlledDocument.documentNumber.asc()))).scalars().all()
    out = [svc.to_dict(d) for d in rows]
    if q:
        ql = q.lower()
        out = [d for d in out if ql in f"{d['documentNumber']} {d['title']} {d['category']}".lower()]
    return {"plantId": plantId, "count": len(out), "documents": out}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: CreateBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.CREATE")
    doc = await svc.create_document(db, user=user, data=body.model_dump())
    await db.commit()
    return svc.to_dict(doc)


# ─── Detail + actions ────────────────────────────────────────────────────


@router.get("/{doc_id}")
async def get_document(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.READ")
    doc = await _load(db, doc_id)
    versions = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.documentId == doc_id).order_by(DocumentVersion.createdAt.asc())
    )).scalars().all()

    name_cache: dict[str, str | None] = {}

    async def nm(uid: str | None) -> str | None:
        if not uid:
            return None
        if uid not in name_cache:
            u = await db.get(User, uid)
            name_cache[uid] = u.name if u else None
        return name_cache[uid]

    version_out = []
    for v in versions:
        vd = svc.version_dict(v)
        vd["authoredByName"] = await nm(v.authoredByUserId)
        vd["approvedByName"] = await nm(v.approvedByUserId)
        vd["signatures"] = await part11.signatures_for(db, "document_version", v.id, current_snapshot=svc.version_snapshot(v, doc))
        version_out.append(vd)

    acks = await part11.signatures_for(db, "document_ack", doc.id)
    audit = await part11.audit_for(db, "document", doc.id)
    # Resolve distribution names
    dist = []
    for e in (doc.distributionList or []):
        dist.append({**e, "name": await nm(e.get("userId"))})

    return {
        "document": svc.to_dict(doc),
        "versions": version_out,
        "distribution": dist,
        "acknowledgments": acks,
        "auditTrail": audit,
    }


@router.post("/{doc_id}/submit")
async def submit(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.CREATE")
    doc = await _load(db, doc_id)
    try:
        await svc.submit_for_review(db, doc=doc, user=user)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return svc.to_dict(doc)


@router.post("/{doc_id}/technical-review")
async def technical_review(doc_id: str, body: PwBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.REVIEW")
    doc = await _load(db, doc_id)
    try:
        await svc.technical_review(db, doc=doc, user=user, password=body.password, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return svc.to_dict(doc)


@router.post("/{doc_id}/qa-review")
async def qa_review(doc_id: str, body: PwBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.REVIEW")
    doc = await _load(db, doc_id)
    try:
        await svc.qa_review(db, doc=doc, user=user, password=body.password, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return svc.to_dict(doc)


@router.post("/{doc_id}/approve")
async def approve(doc_id: str, body: ApproveBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.APPROVE")
    doc = await _load(db, doc_id)
    try:
        await svc.approve(db, doc=doc, user=user, password=body.password, effective_from=body.effectiveFrom, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return svc.to_dict(doc)


@router.post("/{doc_id}/make-effective")
async def make_effective(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.APPROVE")
    doc = await _load(db, doc_id)
    try:
        await svc.make_effective(db, doc=doc, user=user)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return svc.to_dict(doc)


@router.post("/{doc_id}/acknowledge")
async def acknowledge(doc_id: str, body: PwBody, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.READ")
    doc = await _load(db, doc_id)
    try:
        await svc.acknowledge(db, doc=doc, user=user, password=body.password, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(doc)


@router.post("/{doc_id}/revise", status_code=status.HTTP_201_CREATED)
async def revise(doc_id: str, body: ReviseBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.CREATE")
    doc = await _load(db, doc_id)
    v = await svc.revise(db, doc=doc, user=user, change_summary=body.changeSummary, file_hash=body.documentFileHash)
    await db.commit()
    return {"document": svc.to_dict(doc), "newVersion": v.version}


@router.post("/{doc_id}/obsolete")
async def obsolete(doc_id: str, body: ObsoleteBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "DOCUMENT.APPROVE")
    doc = await _load(db, doc_id)
    await svc.obsolete(db, doc=doc, user=user, reason=body.reason)
    await db.commit()
    return svc.to_dict(doc)
