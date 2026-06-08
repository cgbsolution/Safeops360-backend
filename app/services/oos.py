"""OOS Investigation service (Pharma IMS Module 3).

FDA two-phase OOS protocol. Phase 1 (lab investigation) and Phase 2 (full
manufacturing investigation) are each e-signed (21 CFR Part 11). A Phase 2
"manufacturing_cause_identified" conclusion spawns a Deviation via the canonical
deviation service and links it bidirectionally. Batch disposition is e-signed
and closes the OOS. Service functions flush; the router commits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oos import OosInvestigation
from app.models.plant import Plant
from app.models.user import User
from app.services import part11

OPEN_STATUSES = ("phase_1_in_progress", "phase_2_in_progress", "batch_disposition_pending", "qa_review", "escalated")
_DEV_SEVERITY = {"critical": "critical", "major": "major", "minor": "minor"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def oos_snapshot(o: OosInvestigation) -> dict[str, Any]:
    """Immutable test-identity a signature binds to. In a multi-phase record the
    phase conclusions/disposition are FILLED IN over successive signed steps, so
    binding to them would invalidate an earlier phase's signature when a later
    phase is added. Each signature instead attests (via its MEANING) to the step
    it signed, while the hash binds to the identity + the result under test —
    tampering with what was tested or the reported result invalidates every
    signature. The progressive detail is captured in the immutable audit trail."""
    return {
        "number": o.number, "productName": o.productName, "batchNumber": o.batchNumber,
        "testName": o.testName, "specificationReference": o.specificationReference,
        "specificationLimit": o.specificationLimit, "initialResult": o.initialResult, "resultUnit": o.resultUnit,
    }


async def _plant_code(db: AsyncSession, plant_id: str) -> str:
    p = await db.get(Plant, plant_id)
    return (p.code if p and p.code else plant_id[:4]).upper()


async def _next_number(db: AsyncSession) -> str:
    year = _utcnow().year
    n = (await db.execute(select(func.count(OosInvestigation.id)).where(OosInvestigation.number.like(f"OOS-{year}-QC-%")))).scalar_one()
    return f"OOS-{year}-QC-{(n + 1):04d}"


async def create_oos(db: AsyncSession, *, user: User, data: dict[str, Any]) -> OosInvestigation:
    now = _utcnow()
    o = OosInvestigation(
        tenantId=None, number=await _next_number(db), plantId=data["plantId"],
        productName=data["productName"], batchNumber=data["batchNumber"], testName=data["testName"],
        specificationReference=data.get("specificationReference", ""), specificationLimit=data.get("specificationLimit", ""),
        initialResult=data["initialResult"], initialResultNumeric=data.get("initialResultNumeric"),
        resultUnit=data.get("resultUnit", ""), analystUserId=data.get("analystUserId") or user.id,
        analysisDate=data.get("analysisDate") or now, instrumentId=data.get("instrumentId"),
        phase1={"started_at": now.isoformat(), "checks_performed": []},
        status="phase_1_in_progress", createdByUserId=user.id,
    )
    db.add(o)
    await db.flush()
    await part11.write_audit(db, record_type="oos", record_id=o.id, record_number=o.number,
                             event_type="created", user=user, new_value=f"{o.testName} OOS for {o.batchNumber}", reason="OOS logged")
    return o


async def record_phase1(
    db: AsyncSession, *, o: OosInvestigation, user: User, password: str, ip: str | None,
    checks: list[dict] | None, assignable_cause_found: bool, assignable_cause_description: str,
    result_invalidated: bool, retest_authorized: bool, retest_results: list[dict] | None, conclusion: str,
) -> OosInvestigation:
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — Phase 1 not signed.")
    now = _utcnow()
    o.phase1 = {
        "started_at": (o.phase1 or {}).get("started_at", now.isoformat()),
        "checks_performed": checks or [], "assignable_cause_found": assignable_cause_found,
        "assignable_cause_description": assignable_cause_description, "result_invalidated": result_invalidated,
        "retest_authorized": retest_authorized, "retest_results": retest_results or [],
        "phase_1_conclusion": conclusion, "completed_at": now.isoformat(), "investigator_user_id": user.id,
    }
    o.phase1Conclusion = conclusion
    o.phase1ByUserId = user.id
    o.phase1CompletedAt = now
    await part11.sign(db, user=user, record_type="oos", record_id=o.id, record_number=o.number,
                      meaning="Phase 1 — Laboratory Investigation Approved", record_snapshot=oos_snapshot(o), ip=ip)
    new_status = "phase_2_in_progress" if conclusion in ("no_laboratory_error_proceeds_to_phase_2", "retest_confirms_oos") else "batch_disposition_pending"
    old = o.status
    o.status = new_status
    await part11.write_audit(db, record_type="oos", record_id=o.id, record_number=o.number,
                             event_type="status_changed", user=user, field_name="status", old_value=old, new_value=new_status,
                             reason=f"Phase 1 complete: {conclusion}")
    return o


async def record_phase2(
    db: AsyncSession, *, o: OosInvestigation, user: User, password: str, ip: str | None,
    root_cause_category: str, root_cause_description: str, conclusion: str,
    spawn_deviation: bool = True, deviation_severity: str = "major",
) -> OosInvestigation:
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — Phase 2 not signed.")
    now = _utcnow()
    o.phase2 = {
        "started_at": now.isoformat(), "root_cause_category": root_cause_category,
        "root_cause_description": root_cause_description, "phase_2_conclusion": conclusion,
        "completed_at": now.isoformat(), "investigator_user_id": user.id,
    }
    o.phase2Conclusion = conclusion
    o.phase2ByUserId = user.id
    o.phase2CompletedAt = now
    o.rootCauseCategory = root_cause_category
    o.rootCauseDescription = root_cause_description

    if conclusion == "manufacturing_cause_identified" and spawn_deviation and not o.deviationId:
        from app.services import deviation as dsvc
        dev = await dsvc.create_deviation(db, user=user, data={
            "plantId": o.plantId,
            "title": f"OOS {o.number} — {o.testName} manufacturing cause ({o.productName} {o.batchNumber})",
            "category": "laboratory", "severity": _DEV_SEVERITY.get(deviation_severity, "major"), "type": "unplanned",
            "description": (f"OOS investigation {o.number} Phase 2 identified a manufacturing root cause for "
                            f"{o.testName} (result {o.initialResult} vs spec {o.specificationLimit}). {root_cause_description}"),
            "department": "Quality Control", "area": "QC Lab", "detectionMethod": "oos_investigation",
            "affectedProductName": o.productName, "affectedBatchNumbers": [o.batchNumber],
            "approvedProcessReference": o.specificationReference,
        })
        o.deviationRaised = True
        o.deviationId = dev.id
        o.deviationNumber = dev.number
        await part11.write_audit(db, record_type="oos", record_id=o.id, record_number=o.number,
                                 event_type="modified", user=user, field_name="deviationId", new_value=dev.number,
                                 reason="Deviation spawned from OOS Phase 2 manufacturing cause")

    await part11.sign(db, user=user, record_type="oos", record_id=o.id, record_number=o.number,
                      meaning="Phase 2 — Full Investigation Approved", record_snapshot=oos_snapshot(o), ip=ip)
    old = o.status
    o.status = "batch_disposition_pending"
    await part11.write_audit(db, record_type="oos", record_id=o.id, record_number=o.number,
                             event_type="status_changed", user=user, field_name="status", old_value=old,
                             new_value="batch_disposition_pending", reason=f"Phase 2 complete: {conclusion}")
    return o


async def record_disposition(
    db: AsyncSession, *, o: OosInvestigation, user: User, password: str, ip: str | None,
    disposition: str, justification: str,
) -> OosInvestigation:
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — disposition not signed.")
    o.batchDisposition = disposition
    o.batchDispositionJustification = justification
    o.batchDispositionByUserId = user.id
    o.batchDispositionAt = _utcnow()
    await part11.sign(db, user=user, record_type="oos", record_id=o.id, record_number=o.number,
                      meaning=f"Batch disposition: {disposition}", record_snapshot=oos_snapshot(o), ip=ip)
    old = o.status
    o.status = "closed"
    o.closedAt = _utcnow()
    await part11.write_audit(db, record_type="oos", record_id=o.id, record_number=o.number,
                             event_type="closed", user=user, field_name="batchDisposition", old_value=old,
                             new_value=disposition, reason=justification or "Batch dispositioned; OOS closed")
    return o


def to_dict(o: OosInvestigation) -> dict[str, Any]:
    return {
        "id": o.id, "number": o.number, "plantId": o.plantId, "status": o.status,
        "productName": o.productName, "batchNumber": o.batchNumber, "testName": o.testName,
        "specificationReference": o.specificationReference, "specificationLimit": o.specificationLimit,
        "initialResult": o.initialResult, "resultUnit": o.resultUnit, "analystUserId": o.analystUserId,
        "analysisDate": _iso(o.analysisDate), "instrumentId": o.instrumentId,
        "phase1": o.phase1, "phase1Conclusion": o.phase1Conclusion, "phase1CompletedAt": _iso(o.phase1CompletedAt),
        "phase2": o.phase2, "phase2Conclusion": o.phase2Conclusion, "phase2CompletedAt": _iso(o.phase2CompletedAt),
        "deviationRaised": o.deviationRaised, "deviationId": o.deviationId, "deviationNumber": o.deviationNumber,
        "rootCauseCategory": o.rootCauseCategory, "rootCauseDescription": o.rootCauseDescription,
        "batchDisposition": o.batchDisposition, "batchDispositionJustification": o.batchDispositionJustification,
        "createdAt": _iso(o.createdAt), "closedAt": _iso(o.closedAt),
    }


async def dashboard(db: AsyncSession, *, plant_id: str) -> dict[str, Any]:
    rows = (await db.execute(select(OosInvestigation).where(OosInvestigation.plantId == plant_id))).scalars().all()
    by_status: dict[str, int] = {}
    open_count = phase2 = dev_linked = 0
    for o in rows:
        by_status[o.status] = by_status.get(o.status, 0) + 1
        if o.status in OPEN_STATUSES:
            open_count += 1
        if o.status == "phase_2_in_progress":
            phase2 += 1
        if o.deviationId:
            dev_linked += 1
    return {"plantId": plant_id, "total": len(rows), "open": open_count, "inPhase2": phase2,
            "deviationLinked": dev_linked, "byStatus": by_status}
