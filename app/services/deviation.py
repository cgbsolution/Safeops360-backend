"""Deviation Management service (Pharma IMS Module 1).

The workflow engine: detect → QA classify → impact assessment → investigation →
batch disposition (e-signed) → CAPA → closure (e-signed). Every transition and
field change writes a 21 CFR Part 11 GMP audit entry (old→new value + reason).
Batch disposition and closure require a re-authenticated electronic signature.

Service functions FLUSH but do not COMMIT — the router owns the transaction so a
CAPA spawn and the deviation update land atomically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deviation import Deviation
from app.models.plant import Plant
from app.models.user import User
from app.services import part11

# Investigation SLA (calendar days) by severity — tighter for higher risk.
INVESTIGATION_SLA_DAYS = {"critical": 10, "major": 20, "minor": 30}

# Deviation severity → CAPA severity / priority.
_CAPA_SEVERITY = {"critical": "CRITICAL", "major": "HIGH", "minor": "MODERATE"}
_CAPA_PRIORITY = {"critical": "URGENT", "major": "HIGH", "minor": "MODERATE"}

OPEN_STATUSES = (
    "draft", "submitted", "under_qa_review", "impact_assessment_pending",
    "investigation_in_progress", "investigation_complete_pending_qa_review",
    "batch_disposition_pending", "capa_pending", "escalated",
)
CLOSED_STATUSES = ("closed_no_capa", "closed_with_capa", "closed_rejected")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


async def _plant_code(db: AsyncSession, plant_id: str) -> str:
    plant = await db.get(Plant, plant_id)
    return (plant.code if plant and plant.code else plant_id[:4]).upper()


async def _next_number(db: AsyncSession, plant_code: str) -> str:
    year = _utcnow().year
    count = (
        await db.execute(
            select(func.count(Deviation.id)).where(Deviation.number.like(f"DEV-{year}-{plant_code}-%"))
        )
    ).scalar_one()
    return f"DEV-{year}-{plant_code}-{(count + 1):04d}"


async def _transition(db: AsyncSession, dev: Deviation, new_status: str, user: User, reason: str) -> None:
    old = dev.status
    dev.status = new_status
    dev.versionNumber = (dev.versionNumber or 1) + 1
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="status_changed", user=user, field_name="status",
        old_value=old, new_value=new_status, reason=reason,
    )


# ─── Create ──────────────────────────────────────────────────────────────


async def create_deviation(db: AsyncSession, *, user: User, data: dict[str, Any]) -> Deviation:
    plant_id = data["plantId"]
    plant_code = await _plant_code(db, plant_id)
    now = _utcnow()
    dev = Deviation(
        tenantId=None,
        number=await _next_number(db, plant_code),
        title=data["title"],
        description=data["description"],
        type=data.get("type", "unplanned"),
        category=data["category"],
        severity=data.get("severity", "minor"),
        plantId=plant_id,
        department=data.get("department", ""),
        area=data.get("area", ""),
        detectionDate=data.get("detectionDate") or now,
        occurrenceDate=data.get("occurrenceDate"),
        detectionMethod=data.get("detectionMethod", ""),
        detectedByUserId=user.id,
        affectedProductName=data.get("affectedProductName"),
        affectedProductCode=data.get("affectedProductCode"),
        affectedBatchNumbers=data.get("affectedBatchNumbers", []),
        affectedBatchSize=data.get("affectedBatchSize"),
        batchStatusAtDetection=data.get("batchStatusAtDetection"),
        approvedProcessReference=data.get("approvedProcessReference", ""),
        approvedProcessVersion=data.get("approvedProcessVersion", ""),
        immediateActionsTaken=data.get("immediateActionsTaken", ""),
        batchQuarantined=data.get("batchQuarantined", False),
        productionStopped=data.get("productionStopped", False),
        trendingTags=data.get("trendingTags", []),
        status="submitted",
        createdByUserId=user.id,
        versionNumber=1,
    )
    db.add(dev)
    await db.flush()
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="created", user=user, new_value=dev.title,
        reason="Deviation reported",
    )
    return dev


# ─── QA classification ───────────────────────────────────────────────────


async def qa_classify(
    db: AsyncSession, *, dev: Deviation, user: User,
    type_: str | None = None, category: str | None = None, severity: str | None = None,
    investigator_user_id: str | None = None, batch_quarantined: bool | None = None,
) -> Deviation:
    now = _utcnow()
    if type_:
        dev.type = type_
    if category:
        dev.category = category
    if severity:
        dev.severity = severity
    if batch_quarantined is not None:
        dev.batchQuarantined = batch_quarantined
    dev.qaClassifiedByUserId = user.id
    dev.qaClassifiedAt = now
    if investigator_user_id:
        dev.investigationAssignedToUserId = investigator_user_id
        sla = INVESTIGATION_SLA_DAYS.get(dev.severity, 30)
        dev.investigationDueDate = now + timedelta(days=sla)
    await _transition(db, dev, "investigation_in_progress", user,
                      f"QA classified severity={dev.severity}; investigation assigned")
    return dev


# ─── Impact assessment ───────────────────────────────────────────────────


async def record_impact(db: AsyncSession, *, dev: Deviation, user: User, impact: dict[str, Any]) -> Deviation:
    impact = dict(impact)
    impact["assessed_by_user_id"] = user.id
    impact["assessed_at"] = _utcnow().isoformat()
    dev.impactAssessment = impact
    dev.regulatoryReportable = bool(impact.get("regulatory_reportable", dev.regulatoryReportable))
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="modified", user=user, field_name="impactAssessment",
        new_value=impact.get("quality_impact"), reason="Impact assessment recorded",
    )
    return dev


# ─── Investigation ───────────────────────────────────────────────────────


async def record_investigation(
    db: AsyncSession, *, dev: Deviation, user: User,
    root_cause_category: str, root_cause_description: str, methodology: str | None = None,
    contributing_factors: list[str] | None = None, similar_past: list[str] | None = None,
    capa_required: bool = False,
) -> Deviation:
    dev.rootCauseCategory = root_cause_category
    dev.rootCauseDescription = root_cause_description
    dev.rootCauseMethodology = methodology
    dev.contributingFactors = contributing_factors or []
    dev.similarPastDeviations = similar_past or []
    dev.capaRequired = capa_required
    dev.investigationCompletedAt = _utcnow()
    if similar_past:
        dev.isRecurring = True
        dev.previousDeviationNumbers = similar_past
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="modified", user=user, field_name="rootCause",
        new_value=root_cause_category, reason="Investigation completed",
    )
    await _transition(db, dev, "investigation_complete_pending_qa_review", user,
                      "Investigation complete — pending QA review")
    return dev


# ─── Batch disposition (electronic signature required) ───────────────────


async def record_disposition(
    db: AsyncSession, *, dev: Deviation, user: User,
    recommendation: str, justification: str, password: str, ip: str | None = None,
) -> Deviation:
    # 21 CFR Part 11: re-authenticate FIRST, then apply the decision, then sign
    # over the updated record so the signature binds to the disposition made.
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — signature not applied.")
    old = dev.batchDispositionRecommendation
    dev.batchDispositionRecommendation = recommendation
    dev.batchDispositionJustification = justification
    dev.batchDispositionDecidedByUserId = user.id
    dev.batchDispositionDecidedAt = _utcnow()
    await part11.sign(
        db, user=user, record_type="deviation", record_id=dev.id, record_number=dev.number,
        meaning=f"Batch disposition: {recommendation}",
        record_snapshot=part11.deviation_snapshot(dev), ip=ip,
    )
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="modified", user=user, field_name="batchDisposition",
        old_value=old, new_value=recommendation, reason=justification or "Batch disposition decided",
    )
    target = "capa_pending" if dev.capaRequired and not dev.capaId else "batch_disposition_pending"
    await _transition(db, dev, target, user, "Batch disposition signed")
    return dev


# ─── Raise / link a CAPA (reuses the canonical CAPA-creation path) ───────


async def raise_capa(db: AsyncSession, *, dev: Deviation, user: User, primary_owner_user_id: str | None = None) -> dict[str, Any]:
    # Imported lazily to avoid any import cycle with the CAPA router.
    from app.routers.capa import create_capa
    from app.schemas.capa import CapaCreate

    problem = (
        f"Deviation {dev.number} ({dev.category}, {dev.severity} severity). "
        f"{dev.description} Root cause: {dev.rootCauseDescription or 'see investigation'}. "
        f"This CAPA addresses the systemic cause and prevents recurrence."
    )
    if len(problem) < 50:
        problem += " GMP corrective and preventive action required per ICH Q10."

    payload = CapaCreate(
        plantId=dev.plantId,
        sourceTypeCode="DEVIATION",
        sourceReferenceId=dev.id,
        sourceReferenceUrl=f"/deviations/{dev.id}",
        sourceReferenceSummary=f"Deviation {dev.number}: {dev.title}",
        sourceMetadata={"deviationNumber": dev.number, "category": dev.category, "severity": dev.severity},
        title=dev.title,
        problemDescription=problem,
        detectionMethod=dev.detectionMethod or None,
        detectedAt=_aware(dev.detectionDate) or _utcnow(),
        affectedDepartments=[dev.department] if dev.department else None,
        primaryCategory="Quality / GMP Deviation",
        severity=_CAPA_SEVERITY.get(dev.severity, "MODERATE"),
        priority=_CAPA_PRIORITY.get(dev.severity, "MODERATE"),
        primaryOwnerUserId=primary_owner_user_id or dev.investigationAssignedToUserId or user.id,
    )
    capa = await create_capa(payload, user=user, db=db)

    dev.capaRequired = True
    dev.capaId = capa.id
    dev.capaNumber = capa.capaNumber
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="modified", user=user, field_name="capaId",
        new_value=capa.capaNumber, reason="CAPA raised from deviation",
    )
    if dev.status == "capa_pending":
        await _transition(db, dev, "batch_disposition_pending", user, "CAPA linked")
    return {"capaId": capa.id, "capaNumber": capa.capaNumber}


# ─── Closure (electronic signature required) ─────────────────────────────


async def close_deviation(
    db: AsyncSession, *, dev: Deviation, user: User, password: str, ip: str | None = None,
) -> Deviation:
    if dev.batchDispositionRecommendation is None:
        raise ValueError("Cannot close — batch disposition not yet decided.")
    if dev.capaRequired and not dev.capaId:
        raise ValueError("Cannot close — CAPA is required but not yet raised.")
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — signature not applied.")

    dev.closedAt = _utcnow()
    await part11.sign(
        db, user=user, record_type="deviation", record_id=dev.id, record_number=dev.number,
        meaning="Reviewed and Approved — Deviation Closure",
        record_snapshot=part11.deviation_snapshot(dev), ip=ip,
    )
    target = "closed_with_capa" if dev.capaId else "closed_no_capa"
    await _transition(db, dev, target, user, "Deviation closed")
    await part11.write_audit(
        db, record_type="deviation", record_id=dev.id, record_number=dev.number,
        event_type="closed", user=user, reason="Deviation closed",
    )
    return dev


async def reject_deviation(db: AsyncSession, *, dev: Deviation, user: User, reason: str) -> Deviation:
    dev.closedAt = _utcnow()
    await _transition(db, dev, "closed_rejected", user, reason or "Rejected by QA")
    return dev


# ─── Serialisation ───────────────────────────────────────────────────────


def to_dict(dev: Deviation) -> dict[str, Any]:
    now = _utcnow()
    due = _aware(dev.investigationDueDate)
    overdue = bool(due and dev.investigationCompletedAt is None and now > due and dev.status in OPEN_STATUSES)
    return {
        "id": dev.id,
        "number": dev.number,
        "title": dev.title,
        "description": dev.description,
        "type": dev.type,
        "category": dev.category,
        "severity": dev.severity,
        "status": dev.status,
        "plantId": dev.plantId,
        "department": dev.department,
        "area": dev.area,
        "detectionDate": _iso(dev.detectionDate),
        "occurrenceDate": _iso(dev.occurrenceDate),
        "detectionMethod": dev.detectionMethod,
        "detectedByUserId": dev.detectedByUserId,
        "affectedProductName": dev.affectedProductName,
        "affectedBatchNumbers": dev.affectedBatchNumbers or [],
        "batchStatusAtDetection": dev.batchStatusAtDetection,
        "approvedProcessReference": dev.approvedProcessReference,
        "approvedProcessVersion": dev.approvedProcessVersion,
        "immediateActionsTaken": dev.immediateActionsTaken,
        "batchQuarantined": dev.batchQuarantined,
        "productionStopped": dev.productionStopped,
        "impactAssessment": dev.impactAssessment,
        "batchDispositionRecommendation": dev.batchDispositionRecommendation,
        "batchDispositionJustification": dev.batchDispositionJustification,
        "investigationAssignedToUserId": dev.investigationAssignedToUserId,
        "investigationDueDate": _iso(dev.investigationDueDate),
        "investigationOverdue": overdue,
        "investigationCompletedAt": _iso(dev.investigationCompletedAt),
        "rootCauseCategory": dev.rootCauseCategory,
        "rootCauseDescription": dev.rootCauseDescription,
        "rootCauseMethodology": dev.rootCauseMethodology,
        "contributingFactors": dev.contributingFactors or [],
        "capaRequired": dev.capaRequired,
        "capaId": dev.capaId,
        "capaNumber": dev.capaNumber,
        "regulatoryReportable": dev.regulatoryReportable,
        "isRecurring": dev.isRecurring,
        "trendingTags": dev.trendingTags or [],
        "versionNumber": dev.versionNumber,
        "createdAt": _iso(dev.createdAt),
        "closedAt": _iso(dev.closedAt),
    }


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


# ─── Trending (feeds Management Review — ICH Q10) ────────────────────────


async def trending(db: AsyncSession, *, plant_id: str) -> dict[str, Any]:
    rows = (await db.execute(select(Deviation).where(Deviation.plantId == plant_id))).scalars().all()
    now = _utcnow()
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_month: dict[str, int] = {}
    open_count = overdue_invest = recurring = closed_in_sla = closed_total = 0
    for d in rows:
        by_category[d.category] = by_category.get(d.category, 0) + 1
        by_severity[d.severity] = by_severity.get(d.severity, 0) + 1
        by_status[d.status] = by_status.get(d.status, 0) + 1
        if d.detectionDate:
            mk = _aware(d.detectionDate).strftime("%Y-%m")
            by_month[mk] = by_month.get(mk, 0) + 1
        if d.status in OPEN_STATUSES:
            open_count += 1
            due = _aware(d.investigationDueDate)
            if due and d.investigationCompletedAt is None and now > due:
                overdue_invest += 1
        if d.isRecurring:
            recurring += 1
        if d.status in CLOSED_STATUSES:
            closed_total += 1
            due = _aware(d.investigationDueDate)
            comp = _aware(d.investigationCompletedAt)
            if due and comp and comp <= due:
                closed_in_sla += 1
    return {
        "plantId": plant_id,
        "total": len(rows),
        "open": open_count,
        "overdueInvestigations": overdue_invest,
        "recurring": recurring,
        "closureInSlaRate": round((closed_in_sla / closed_total) * 100, 1) if closed_total else 0.0,
        "byCategory": by_category,
        "bySeverity": by_severity,
        "byStatus": by_status,
        "byMonth": dict(sorted(by_month.items())),
    }
