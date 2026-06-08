"""Deviation Management router (Pharma IMS Module 1). Mounts at /api/deviations.

The full GMP workflow + trending + taxonomy. Batch disposition and closure
re-authenticate and apply a 21 CFR Part 11 electronic signature. RBAC: reads
need DEVIATION.READ; create DEVIATION.CREATE; investigation steps
DEVIATION.UPDATE; disposition/reject DEVIATION.APPROVE; closure DEVIATION.CLOSE.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.deviation import Deviation
from app.models.user import User
from app.services import deviation as svc
from app.services import part11
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/deviations", tags=["deviations"])


# ─── Reference data (spec §8.1 / §8.2) ───────────────────────────────────

DEVIATION_CATEGORIES = [
    {"code": "manufacturing_process", "name": "Manufacturing Process", "subcategories": ["temperature_excursion", "time_excursion", "mixing_deviation", "filling_deviation", "compression_deviation", "coating_deviation", "sterilization_deviation"]},
    {"code": "environmental_condition", "name": "Environmental Monitoring", "subcategories": ["temperature_rh", "particulate", "microbial", "pressure_differential"]},
    {"code": "equipment_failure", "name": "Equipment Failure", "subcategories": ["breakdown", "calibration_due", "cleaning_failure", "software_error"]},
    {"code": "utility_failure", "name": "Utility Failure", "subcategories": ["purified_water", "wfi", "hvac", "compressed_air", "nitrogen", "clean_steam"]},
    {"code": "material_issue", "name": "Raw Material / Packaging", "subcategories": ["non_conforming_incoming", "storage_excursion", "contamination", "wrong_material"]},
    {"code": "laboratory", "name": "Laboratory", "subcategories": ["oos_result", "oot_result", "instrument_failure", "reagent_failure", "sampling_error"]},
    {"code": "documentation", "name": "Documentation", "subcategories": ["sop_not_followed", "incomplete_record", "transcription_error", "wrong_version_used"]},
    {"code": "cleaning_validation", "name": "Cleaning Validation", "subcategories": ["residue_limit_exceeded", "visual_fail", "swab_oos"]},
    {"code": "human_error", "name": "Human Error", "subcategories": []},
    {"code": "it_system", "name": "IT System", "subcategories": []},
    {"code": "other", "name": "Other", "subcategories": []},
]
ROOT_CAUSE_CATEGORIES = [
    {"code": "method_procedure", "name": "Method / Procedure (SOP inadequate or not followed)"},
    {"code": "man_training", "name": "Man / Training (inadequate training or competency)"},
    {"code": "machine_equipment", "name": "Machine / Equipment"},
    {"code": "material", "name": "Material"},
    {"code": "measurement", "name": "Measurement / Calibration"},
    {"code": "environment", "name": "Environment"},
    {"code": "management_system", "name": "Management System"},
    {"code": "other", "name": "Other"},
]
REGULATORY_LIBRARY = [
    {"code": "21CFR211", "name": "21 CFR Part 211 — cGMP for Finished Pharmaceuticals", "authority": "US FDA"},
    {"code": "21CFR11", "name": "21 CFR Part 11 — Electronic Records; Electronic Signatures", "authority": "US FDA"},
    {"code": "EUGMP_CH1", "name": "EU GMP Chapter 1 — Pharmaceutical Quality System", "authority": "EMA"},
    {"code": "EUGMP_A11", "name": "EU GMP Annex 11 — Computerised Systems", "authority": "EMA"},
    {"code": "ICHQ10", "name": "ICH Q10 — Pharmaceutical Quality System", "authority": "ICH"},
    {"code": "ICHQ9", "name": "ICH Q9 — Quality Risk Management", "authority": "ICH"},
    {"code": "SCHEDULE_M", "name": "Schedule M — GMP for Pharmaceutical Products (India)", "authority": "CDSCO"},
]
SEVERITIES = ["critical", "major", "minor"]
DETECTION_METHODS = ["routine_monitoring", "batch_record_review", "in_process_check", "end_product_testing", "complaint", "audit", "oos_investigation", "other"]
DISPOSITIONS = ["release", "reject", "reprocess", "retest_and_release_if_pass", "quarantine_pending_investigation", "destroy"]


# ─── RBAC helper ─────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, code: str) -> None:
    r = await can(db, user.id, code, PermissionContext())
    if not r.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, r.reason or f"Missing permission {code}")


async def _load(db: AsyncSession, deviation_id: str) -> Deviation:
    dev = await db.get(Deviation, deviation_id)
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deviation not found")
    return dev


def _ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


# ─── Request bodies ──────────────────────────────────────────────────────


class CreateBody(BaseModel):
    plantId: str
    title: str = Field(min_length=4)
    description: str = Field(min_length=10)
    type: str = "unplanned"
    category: str
    severity: str = "minor"
    department: str = ""
    area: str = ""
    detectionDate: datetime | None = None
    occurrenceDate: datetime | None = None
    detectionMethod: str = ""
    affectedProductName: str | None = None
    affectedProductCode: str | None = None
    affectedBatchNumbers: list[str] = []
    affectedBatchSize: int | None = None
    batchStatusAtDetection: str | None = None
    approvedProcessReference: str = ""
    approvedProcessVersion: str = ""
    immediateActionsTaken: str = ""
    batchQuarantined: bool = False
    productionStopped: bool = False


class ClassifyBody(BaseModel):
    type: str | None = None
    category: str | None = None
    severity: str | None = None
    investigatorUserId: str | None = None
    batchQuarantined: bool | None = None


class ImpactBody(BaseModel):
    quality_impact: str = "none"
    quality_impact_narrative: str = ""
    patient_safety_impact: str = "none"
    patient_safety_narrative: str = ""
    batches_potentially_affected_count: int = 0
    market_action_required: bool = False
    regulatory_reportable: bool = False
    regulatory_authority: str = ""


class InvestigateBody(BaseModel):
    rootCauseCategory: str
    rootCauseDescription: str = Field(min_length=5)
    methodology: str | None = None
    contributingFactors: list[str] = []
    similarPastDeviations: list[str] = []
    capaRequired: bool = False


class DispositionBody(BaseModel):
    recommendation: str
    justification: str = ""
    password: str


class RaiseCapaBody(BaseModel):
    primaryOwnerUserId: str | None = None


class CloseBody(BaseModel):
    password: str


class RejectBody(BaseModel):
    reason: str = ""


# ─── Static routes (declared before /{id}) ───────────────────────────────


@router.get("/taxonomy")
async def taxonomy(user: User = Depends(get_current_user)) -> dict:
    return {
        "categories": DEVIATION_CATEGORIES,
        "rootCauseCategories": ROOT_CAUSE_CATEGORIES,
        "regulatoryLibrary": REGULATORY_LIBRARY,
        "severities": SEVERITIES,
        "detectionMethods": DETECTION_METHODS,
        "dispositions": DISPOSITIONS,
    }


@router.get("/trending")
async def trending(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.READ")
    return await svc.trending(db, plant_id=plantId)


@router.get("/users")
async def plant_users(
    plantId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lightweight plant roster for investigator / CAPA-owner pickers."""
    await _require(db, user, "DEVIATION.READ")
    rows = (await db.execute(select(User).where(User.plantId == plantId).order_by(User.name.asc()))).scalars().all()
    return {"users": [{"id": u.id, "name": u.name, "role": u.role, "department": u.department or ""} for u in rows]}


@router.get("")
async def list_deviations(
    plantId: str = Query(...),
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = Query(None),
    category: str | None = Query(None),
    openOnly: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.READ")
    stmt = select(Deviation).where(Deviation.plantId == plantId)
    if status_filter:
        stmt = stmt.where(Deviation.status == status_filter)
    if severity:
        stmt = stmt.where(Deviation.severity == severity)
    if category:
        stmt = stmt.where(Deviation.category == category)
    rows = (await db.execute(stmt.order_by(Deviation.createdAt.desc()))).scalars().all()
    out = [svc.to_dict(d) for d in rows]
    if openOnly:
        out = [d for d in out if d["status"] in svc.OPEN_STATUSES]
    return {"plantId": plantId, "count": len(out), "deviations": out}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(
    body: CreateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.CREATE")
    dev = await svc.create_deviation(db, user=user, data=body.model_dump())
    await db.commit()
    return svc.to_dict(dev)


# ─── Detail + workflow actions ───────────────────────────────────────────


@router.get("/{deviation_id}")
async def get_deviation(
    deviation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.READ")
    dev = await _load(db, deviation_id)
    snapshot = part11.deviation_snapshot(dev)
    signatures = await part11.signatures_for(db, "deviation", dev.id, current_snapshot=snapshot)
    audit = await part11.audit_for(db, "deviation", dev.id)

    async def _name(uid: str | None) -> str | None:
        if not uid:
            return None
        u = await db.get(User, uid)
        return u.name if u else None

    return {
        "deviation": svc.to_dict(dev),
        "detectedByName": await _name(dev.detectedByUserId),
        "investigatorName": await _name(dev.investigationAssignedToUserId),
        "qaClassifiedByName": await _name(dev.qaClassifiedByUserId),
        "signatures": signatures,
        "auditTrail": audit,
    }


@router.post("/{deviation_id}/classify")
async def classify(
    deviation_id: str, body: ClassifyBody,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.UPDATE")
    dev = await _load(db, deviation_id)
    await svc.qa_classify(
        db, dev=dev, user=user, type_=body.type, category=body.category, severity=body.severity,
        investigator_user_id=body.investigatorUserId, batch_quarantined=body.batchQuarantined,
    )
    await db.commit()
    return svc.to_dict(dev)


@router.post("/{deviation_id}/impact")
async def impact(
    deviation_id: str, body: ImpactBody,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.UPDATE")
    dev = await _load(db, deviation_id)
    await svc.record_impact(db, dev=dev, user=user, impact=body.model_dump())
    await db.commit()
    return svc.to_dict(dev)


@router.post("/{deviation_id}/investigate")
async def investigate(
    deviation_id: str, body: InvestigateBody,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.UPDATE")
    dev = await _load(db, deviation_id)
    await svc.record_investigation(
        db, dev=dev, user=user, root_cause_category=body.rootCauseCategory,
        root_cause_description=body.rootCauseDescription, methodology=body.methodology,
        contributing_factors=body.contributingFactors, similar_past=body.similarPastDeviations,
        capa_required=body.capaRequired,
    )
    await db.commit()
    return svc.to_dict(dev)


@router.post("/{deviation_id}/disposition")
async def disposition(
    deviation_id: str, body: DispositionBody, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.APPROVE")
    dev = await _load(db, deviation_id)
    try:
        await svc.record_disposition(
            db, dev=dev, user=user, recommendation=body.recommendation,
            justification=body.justification, password=body.password, ip=_ip(request),
        )
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return svc.to_dict(dev)


@router.post("/{deviation_id}/raise-capa")
async def raise_capa(
    deviation_id: str, body: RaiseCapaBody,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.UPDATE")
    dev = await _load(db, deviation_id)
    try:
        result = await svc.raise_capa(db, dev=dev, user=user, primary_owner_user_id=body.primaryOwnerUserId)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not raise CAPA: {e}")
    await db.commit()
    return {**svc.to_dict(dev), "raisedCapa": result}


@router.post("/{deviation_id}/close")
async def close(
    deviation_id: str, body: CloseBody, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.CLOSE")
    dev = await _load(db, deviation_id)
    try:
        await svc.close_deviation(db, dev=dev, user=user, password=body.password, ip=_ip(request))
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return svc.to_dict(dev)


@router.post("/{deviation_id}/reject")
async def reject(
    deviation_id: str, body: RejectBody,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await _require(db, user, "DEVIATION.APPROVE")
    dev = await _load(db, deviation_id)
    await svc.reject_deviation(db, dev=dev, user=user, reason=body.reason)
    await db.commit()
    return svc.to_dict(dev)
