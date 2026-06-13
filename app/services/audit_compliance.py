"""Audit & Compliance Management — service layer.

Functions flush but DO NOT commit; the router commits. Mirrors the pattern used
by the other vertical modules (oos/capa). Schema is owned by Prisma; this layer
reads/writes the SQLAlchemy mirror in app/models/audit_compliance.py.

Lifecycle: schedule -> conduct (partial-save per checkpoint) -> submit (route
failed/partial to auditees + auto-CAPA on critical) -> auditee respond ->
plant-manager review -> close. The `score` snapshot on ComplianceAudit is
recomputed only at submit/review/close; live conduct progress is computed
on-read.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.audit_compliance import (
    AuditCheckpointLibrary,
    AuditCheckpointResponse,
    AuditTemplate,
    ComplianceAudit,
)
from app.models.plant import Plant
from app.models.user import User

MINIMUM_PASS_SCORE = 80.0

# capa_severity_if_triggered (checkpoint) -> CAPA severity
_CAPA_SEVERITY = {"critical": "CRITICAL", "major": "HIGH", "minor": "MODERATE"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _naive(dt: datetime | None) -> datetime | None:
    """Drop tzinfo so naive (asyncpg) and aware datetimes can be compared."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _industry_short(code: str) -> str:
    """GARMENTS_TEXTILE -> GT, MANUFACTURING_GENERIC -> MG."""
    return "".join(part[0] for part in code.split("_") if part)[:3].upper() or "AC"


def _norm_value(value: Any) -> str | None:
    """Map a checkpoint response value into a scoring bucket."""
    if value in ("pass", "yes"):
        return "pass"
    if value == "partial":
        return "partial"
    if value in ("fail", "no"):
        return "fail"
    if value == "na":
        return "na"
    return None


# ─────────────────────────────────────────────────────────────────────
# Serialization
# ─────────────────────────────────────────────────────────────────────


def _response_to_dict(r: AuditCheckpointResponse) -> dict[str, Any]:
    return {
        "id": r.id,
        "checkpointCode": r.checkpointCode,
        "checkpointQuestion": r.checkpointQuestion,
        "guidance": r.guidance,
        "requirementReference": r.requirementReference,
        "standard": r.standard,
        "categoryId": r.categoryId,
        "categoryName": r.categoryName,
        "categoryColor": r.categoryColor,
        "criticality": r.criticality,
        "responseType": r.responseType,
        "sequence": r.sequence,
        "requiresPhotoOnFail": r.requiresPhotoOnFail,
        "autoTriggerCapaOnFail": r.autoTriggerCapaOnFail,
        "capaSeverity": r.capaSeverity,
        "linkedSafeopsModule": r.linkedSafeopsModule,
        "routedToUserId": r.routedToUserId,
        "auditorResponse": r.auditorResponse,
        "auditeeResponse": r.auditeeResponse,
        "plantManagerReview": r.plantManagerReview,
        "capa": r.capa,
        "overallStatus": r.overallStatus,
        "answeredAt": _iso(r.answeredAt),
    }


def _audit_to_dict(a: ComplianceAudit, *, include_responses: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": a.id,
        "auditNumber": a.auditNumber,
        "title": a.title,
        "plantId": a.plantId,
        "templateId": a.templateId,
        "industryCode": a.industryCode,
        "auditType": a.auditType,
        "scopeDepartments": a.scopeDepartments,
        "scopeAreas": a.scopeAreas,
        "scopeDescription": a.scopeDescription,
        "scheduledDate": _iso(a.scheduledDate),
        "scheduledStartTime": a.scheduledStartTime,
        "estimatedDurationHours": a.estimatedDurationHours,
        "leadAuditorUserId": a.leadAuditorUserId,
        "coAuditors": a.coAuditors,
        "auditees": a.auditees,
        "plantManagerUserId": a.plantManagerUserId,
        "status": a.status,
        "actualStartAt": _iso(a.actualStartAt),
        "actualEndAt": _iso(a.actualEndAt),
        "submittedAt": _iso(a.submittedAt),
        "score": a.score,
        "totalCheckpoints": a.totalCheckpoints,
        "answeredCheckpoints": a.answeredCheckpoints,
        "overallCompliancePct": a.overallCompliancePct,
        "auditPassed": a.auditPassed,
        "openCapaCount": a.openCapaCount,
        "criticalFailureCount": a.criticalFailureCount,
        "openingRemarks": a.openingRemarks,
        "closingRemarks": a.closingRemarks,
        "isRecurring": a.isRecurring,
        "createdByUserId": a.createdByUserId,
        "createdAt": _iso(a.createdAt),
        "closedAt": _iso(a.closedAt),
    }
    if include_responses:
        d["responses"] = [
            _response_to_dict(r) for r in sorted(a.responses, key=lambda x: (x.categoryId, x.sequence))
        ]
    return d


# ─────────────────────────────────────────────────────────────────────
# Reference data (libraries + templates)
# ─────────────────────────────────────────────────────────────────────


async def list_libraries(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.isActive.is_(True)).order_by(
                AuditCheckpointLibrary.industryName
            )
        )
    ).scalars().all()
    out = []
    for lib in rows:
        cats = lib.categories or []
        out.append(
            {
                "id": lib.id,
                "industryCode": lib.industryCode,
                "industryName": lib.industryName,
                "version": lib.version,
                "checkpointCount": lib.checkpointCount,
                "categories": [
                    {
                        "category_code": c.get("category_code"),
                        "category_name": c.get("category_name"),
                        "category_color": c.get("category_color"),
                        "category_icon": c.get("category_icon"),
                        "checkpointCount": len(c.get("checkpoints", [])),
                    }
                    for c in cats
                ],
            }
        )
    return out


async def list_templates(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditTemplate).where(AuditTemplate.isActive.is_(True)).order_by(AuditTemplate.name)
        )
    ).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "auditType": t.auditType,
            "baseIndustry": t.baseIndustry,
            "checkpointConfiguration": t.checkpointConfiguration,
            "version": t.version,
        }
        for t in rows
    ]


# ─────────────────────────────────────────────────────────────────────
# List + dashboards
# ─────────────────────────────────────────────────────────────────────


async def list_audits(db: AsyncSession, *, accessible_plants: list[str] | None) -> list[dict[str, Any]]:
    stmt = select(ComplianceAudit).order_by(ComplianceAudit.scheduledDate.desc())
    if accessible_plants is not None:
        stmt = stmt.where(ComplianceAudit.plantId.in_(accessible_plants))
    rows = (await db.execute(stmt)).scalars().all()
    return [_audit_to_dict(a) for a in rows]


async def programme_dashboard(db: AsyncSession, *, accessible_plants: list[str] | None) -> dict[str, Any]:
    stmt = select(ComplianceAudit)
    if accessible_plants is not None:
        stmt = stmt.where(ComplianceAudit.plantId.in_(accessible_plants))
    audits = (await db.execute(stmt)).scalars().all()

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    compliance_values: list[float] = []
    total_open_capa = 0
    total_critical = 0
    open_count = 0
    closed_count = 0
    next_scheduled: dict[str, Any] | None = None
    next_dt: datetime | None = None
    now = _naive(_utcnow())

    for a in audits:
        by_status[a.status] = by_status.get(a.status, 0) + 1
        by_type[a.auditType] = by_type.get(a.auditType, 0) + 1
        if a.overallCompliancePct is not None:
            compliance_values.append(a.overallCompliancePct)
        total_open_capa += a.openCapaCount or 0
        total_critical += a.criticalFailureCount or 0
        if a.status == "closed":
            closed_count += 1
        else:
            open_count += 1
        sched = _naive(a.scheduledDate)
        if a.status == "scheduled" and sched and sched >= now:
            if next_dt is None or sched < next_dt:
                next_dt = sched
                next_scheduled = {
                    "id": a.id,
                    "auditNumber": a.auditNumber,
                    "title": a.title,
                    "auditType": a.auditType,
                    "scheduledDate": _iso(a.scheduledDate),
                }

    avg_compliance = round(sum(compliance_values) / len(compliance_values), 1) if compliance_values else None

    return {
        "total": len(audits),
        "open": open_count,
        "closed": closed_count,
        "averageCompliancePct": avg_compliance,
        "openCapas": total_open_capa,
        "criticalFindings": total_critical,
        "byStatus": by_status,
        "byType": by_type,
        "nextScheduled": next_scheduled,
    }


async def _load_audit(db: AsyncSession, audit_id: str, *, with_responses: bool = False) -> ComplianceAudit | None:
    stmt = select(ComplianceAudit).where(ComplianceAudit.id == audit_id)
    if with_responses:
        stmt = stmt.options(selectinload(ComplianceAudit.responses))
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_audit(db: AsyncSession, audit_id: str) -> dict[str, Any] | None:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        return None
    d = _audit_to_dict(audit, include_responses=True)
    d["progress"] = _live_progress(audit.responses)
    return d


def _live_progress(responses: list[AuditCheckpointResponse]) -> dict[str, Any]:
    total = len(responses)
    answered = 0
    cat_map: dict[str, dict[str, Any]] = {}
    for r in responses:
        cat = cat_map.setdefault(
            r.categoryId,
            {"categoryId": r.categoryId, "categoryName": r.categoryName, "categoryColor": r.categoryColor,
             "total": 0, "answered": 0, "failed": 0, "partial": 0},
        )
        cat["total"] += 1
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        if val is not None:
            answered += 1
            cat["answered"] += 1
            if val == "fail":
                cat["failed"] += 1
            elif val == "partial":
                cat["partial"] += 1
    return {
        "total": total,
        "answered": answered,
        "completionPct": round(answered / total * 100, 1) if total else 0,
        "categories": sorted(cat_map.values(), key=lambda c: c["categoryName"]),
    }


def _compute_score(audit: ComplianceAudit, responses: list[AuditCheckpointResponse]) -> dict[str, Any]:
    passed = partial = failed = na = answered = 0
    crit_fail = major_fail = minor_fail = 0
    cat_scores: dict[str, dict[str, Any]] = {}

    for r in responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        cat = cat_scores.setdefault(
            r.categoryId,
            {"category_id": r.categoryId, "category_name": r.categoryName, "total": 0,
             "passed": 0, "partial": 0, "failed": 0, "na": 0},
        )
        cat["total"] += 1
        if val is None:
            continue
        answered += 1
        if val == "pass":
            passed += 1
            cat["passed"] += 1
        elif val == "partial":
            partial += 1
            cat["partial"] += 1
        elif val == "fail":
            failed += 1
            cat["failed"] += 1
            if r.criticality == "critical":
                crit_fail += 1
            elif r.criticality == "major":
                major_fail += 1
            else:
                minor_fail += 1
        elif val == "na":
            na += 1
            cat["na"] += 1

    assessable = passed + partial + failed
    overall = round((passed + 0.5 * partial) / assessable * 100, 1) if assessable else 0.0

    category_scores = []
    for c in cat_scores.values():
        c_assess = c["passed"] + c["partial"] + c["failed"]
        c["score_pct"] = round((c["passed"] + 0.5 * c["partial"]) / c_assess * 100, 1) if c_assess else 0.0
        category_scores.append(c)

    audit_passed = crit_fail == 0 and overall >= MINIMUM_PASS_SCORE

    return {
        "total_checkpoints": len(responses),
        "answered": answered,
        "passed": passed,
        "partially_passed": partial,
        "failed": failed,
        "not_applicable": na,
        "overall_score_pct": overall,
        "category_scores": sorted(category_scores, key=lambda c: c["category_name"]),
        "critical_failures": crit_fail,
        "major_failures": major_fail,
        "minor_failures": minor_fail,
        "audit_passed": audit_passed,
    }


async def audit_dashboard(db: AsyncSession, audit_id: str) -> dict[str, Any] | None:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        return None
    score = _compute_score(audit, audit.responses)
    crit_total = sum(1 for r in audit.responses if r.criticality == "critical")
    crit_compliant = crit_total - score["critical_failures"]
    return {
        "auditId": audit.id,
        "auditNumber": audit.auditNumber,
        "title": audit.title,
        "status": audit.status,
        "score": score,
        "criticalCompliance": {
            "total": crit_total,
            "compliant": crit_compliant,
            "pct": round(crit_compliant / crit_total * 100, 1) if crit_total else 100.0,
        },
        "donut": {
            "pass": score["passed"],
            "partial": score["partially_passed"],
            "fail": score["failed"],
            "na": score["not_applicable"],
            "not_answered": score["total_checkpoints"] - score["answered"],
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Create (materialize checkpoint rows)
# ─────────────────────────────────────────────────────────────────────


async def _next_number(db: AsyncSession, industry_code: str, plant_code: str) -> str:
    year = _utcnow().year
    short = _industry_short(industry_code)
    count = (
        await db.execute(select(func.count(ComplianceAudit.id)).where(ComplianceAudit.plantId.isnot(None)))
    ).scalar_one() or 0
    return f"AUD-{short}-{year}-{plant_code}-{(count + 1):04d}"


def _route_for_category(category_code: str, auditees: list[dict[str, Any]]) -> str | None:
    for a in auditees:
        cats = a.get("responsibleCategories") or []
        if category_code in cats:
            return a.get("userId")
    return None


async def create_audit(db: AsyncSession, *, user: User, data: dict[str, Any]) -> ComplianceAudit:
    industry_code = data.get("industryCode")
    audit_type = data.get("auditType") or "compliance_audit"
    config: dict[str, Any] = {"mode": "all"}

    template_id = data.get("templateId")
    if template_id:
        template = await db.get(AuditTemplate, template_id)
        if template is None:
            raise ValueError("Invalid templateId")
        industry_code = template.baseIndustry
        audit_type = data.get("auditType") or template.auditType
        config = template.checkpointConfiguration or {"mode": "all"}

    if not industry_code:
        raise ValueError("industryCode or templateId is required")

    library = (
        await db.execute(
            select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == industry_code)
        )
    ).scalar_one_or_none()
    if library is None:
        raise ValueError(f"No checkpoint library for industry {industry_code}")

    plant = await db.get(Plant, data["plantId"])
    plant_code = plant.code if plant else "XX"

    auditees = data.get("auditees") or []
    mode = (config or {}).get("mode", "all")
    subset_codes = set((config or {}).get("codes") or [])
    subset_categories = set((config or {}).get("categories") or [])

    audit = ComplianceAudit(
        auditNumber=await _next_number(db, industry_code, plant_code),
        title=data["title"],
        plantId=data["plantId"],
        templateId=template_id,
        industryCode=industry_code,
        auditType=audit_type,
        scopeDepartments=data.get("scopeDepartments") or [],
        scopeAreas=data.get("scopeAreas") or [],
        scopeDescription=data.get("scopeDescription") or "",
        scheduledDate=data["scheduledDate"],
        scheduledStartTime=data.get("scheduledStartTime") or "09:00",
        estimatedDurationHours=data.get("estimatedDurationHours") or 2,
        leadAuditorUserId=data.get("leadAuditorUserId") or user.id,
        coAuditors=data.get("coAuditors") or [],
        auditees=auditees,
        plantManagerUserId=data.get("plantManagerUserId"),
        status="scheduled",
        openingRemarks=data.get("openingRemarks") or "",
        createdByUserId=user.id,
    )
    db.add(audit)
    await db.flush()

    rows: list[AuditCheckpointResponse] = []
    seq = 0
    for cat in library.categories or []:
        cat_code = cat.get("category_code")
        if mode == "subset" and subset_categories and cat_code not in subset_categories:
            continue
        for cp in cat.get("checkpoints", []):
            code = cp.get("code")
            if mode == "subset" and subset_codes and code not in subset_codes:
                continue
            seq += 1
            rows.append(
                AuditCheckpointResponse(
                    auditId=audit.id,
                    plantId=audit.plantId,
                    checkpointCode=code,
                    checkpointQuestion=cp.get("question", ""),
                    guidance=cp.get("guidance", ""),
                    requirementReference=cp.get("requirement_reference", ""),
                    standard=cp.get("standard", ""),
                    categoryId=cat_code,
                    categoryName=cat.get("category_name", ""),
                    categoryColor=cat.get("category_color", ""),
                    criticality=cp.get("criticality", "major"),
                    responseType=cp.get("response_type", "pass_partial_fail"),
                    sequence=seq,
                    requiresPhotoOnFail=bool(cp.get("requires_photo_on_fail", False)),
                    autoTriggerCapaOnFail=bool(cp.get("auto_trigger_capa_on_fail", False)),
                    capaSeverity=cp.get("capa_severity_if_triggered"),
                    linkedSafeopsModule=cp.get("linked_safeops_module"),
                    routedToUserId=_route_for_category(cat_code, auditees),
                    overallStatus="not_answered",
                )
            )

    db.add_all(rows)
    audit.totalCheckpoints = len(rows)
    await db.flush()
    return audit


# ─────────────────────────────────────────────────────────────────────
# Conduct: partial-save + submit
# ─────────────────────────────────────────────────────────────────────


# camelCase payload key -> stored snake_case key, for partial-merge saves.
_SAVE_KEY_MAP = {
    "value": "value",
    "numericValue": "numeric_value",
    "selectedOptions": "selected_options",
    "textObservation": "text_observation",
    "auditorNotes": "auditor_notes",
    "photos": "photos",
    "evidenceLinks": "evidence_links",
}


async def save_response(db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; checkpoint responses are locked")

    code = payload["checkpointCode"]
    resp = (
        await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit_id)
            .where(AuditCheckpointResponse.checkpointCode == code)
        )
    ).scalar_one_or_none()
    if resp is None:
        raise ValueError(f"Checkpoint {code} not found on this audit")

    now = _utcnow()
    # MERGE only the fields the client actually sent (the router passes
    # exclude_unset), so an observation-only save never wipes the value.
    merged = dict(resp.auditorResponse or {})
    for src, dst in _SAVE_KEY_MAP.items():
        if src in payload:
            merged[dst] = payload[src]
    merged["responded_at"] = now.isoformat()
    merged["is_saved"] = True
    resp.auditorResponse = merged

    val = _norm_value(merged.get("value"))
    if val is not None:
        resp.overallStatus = f"answered_{val}"
        resp.answeredAt = now
    else:
        resp.overallStatus = "not_answered"
        resp.answeredAt = None

    # Flip the audit into conduct on first save.
    if audit.status == "scheduled":
        audit.status = "in_progress"
        if audit.actualStartAt is None:
            audit.actualStartAt = now

    await db.flush()

    # Recompute the answered counter (one indexed count — avoids drift).
    answered = (
        await db.execute(
            select(func.count(AuditCheckpointResponse.id))
            .where(AuditCheckpointResponse.auditId == audit_id)
            .where(AuditCheckpointResponse.overallStatus.notlike("not_answered"))
        )
    ).scalar_one() or 0
    audit.answeredCheckpoints = answered
    await db.flush()

    return {"ok": True, "checkpointCode": code, "overallStatus": resp.overallStatus, "answered": answered}


async def submit_audit(db: AsyncSession, *, user: User, audit_id: str) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status not in ("scheduled", "in_progress"):
        raise ValueError(f"Audit cannot be submitted from status '{audit.status}'")

    now = _utcnow()
    capa_count = 0
    for r in audit.responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        if val in ("fail", "partial"):
            r.overallStatus = "pending_auditee"
            if not r.routedToUserId:
                r.routedToUserId = audit.plantManagerUserId or audit.leadAuditorUserId
        if val == "fail" and r.autoTriggerCapaOnFail:
            spawned = await _spawn_capa(db, user=user, audit=audit, response=r)
            if spawned:
                capa_count += 1

    score = _compute_score(audit, audit.responses)
    audit.score = score
    audit.overallCompliancePct = score["overall_score_pct"]
    audit.auditPassed = score["audit_passed"]
    audit.criticalFailureCount = score["critical_failures"]
    audit.openCapaCount = (audit.openCapaCount or 0) + capa_count
    audit.status = "submitted_pending_response"
    audit.submittedAt = now
    audit.actualEndAt = now
    await db.flush()
    return {"ok": True, "status": audit.status, "capasSpawned": capa_count, "score": score}


async def _spawn_capa(
    db: AsyncSession, *, user: User, audit: ComplianceAudit, response: AuditCheckpointResponse
) -> bool:
    """Auto-spawn a CAPA from a critical checkpoint failure. Best-effort:
    wrapped in a SAVEPOINT so a CAPA failure never blocks the audit submit."""
    if response.capa and response.capa.get("capa_id"):
        return False  # already linked
    try:
        async with db.begin_nested():
            from app.routers.capa import create_capa
            from app.schemas.capa import CapaCreate

            obs = (response.auditorResponse or {}).get("text_observation", "")
            severity = _CAPA_SEVERITY.get(response.capaSeverity or response.criticality, "MODERATE")
            problem = (
                f"Audit {audit.auditNumber} ({audit.auditType}) — checkpoint {response.checkpointCode} "
                f"in category '{response.categoryName}' failed. Question: {response.checkpointQuestion} "
                f"Auditor observation: {obs or 'see audit record'}. "
                f"Requirement: {response.requirementReference or 'n/a'}."
            )
            payload = CapaCreate(
                plantId=audit.plantId,
                sourceTypeCode="AUDIT_INTERNAL",
                sourceReferenceId=response.id,
                sourceReferenceUrl=f"/audit-compliance/{audit.id}",
                sourceReferenceSummary=f"Audit {audit.auditNumber} — {response.checkpointCode} failed",
                sourceMetadata={
                    "auditNumber": audit.auditNumber,
                    "checkpointCode": response.checkpointCode,
                    "criticality": response.criticality,
                    "categoryName": response.categoryName,
                },
                title=f"Audit finding: {response.checkpointQuestion[:90]}",
                problemDescription=problem,
                detectedAt=audit.actualStartAt or _utcnow(),
                primaryCategory="Audit / Compliance Finding",
                severity=severity,
                priority=severity,
                primaryOwnerUserId=response.routedToUserId or audit.plantManagerUserId or audit.leadAuditorUserId,
            )
            capa = await create_capa(payload, user=user, db=db)
            response.capa = {
                "auto_triggered": True,
                "capa_id": capa.id,
                "capa_number": capa.capaNumber,
                "capa_status": capa.state,
                "capa_due_date": _iso(capa.closureTargetDate),
            }
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Auto-CAPA spawn failed for {response.checkpointCode}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────
# Auditee response + plant-manager review + close
# ─────────────────────────────────────────────────────────────────────


async def auditee_respond(db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    code = payload["checkpointCode"]
    resp = (
        await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit_id)
            .where(AuditCheckpointResponse.checkpointCode == code)
        )
    ).scalar_one_or_none()
    if resp is None:
        raise ValueError(f"Checkpoint {code} not found")
    if resp.overallStatus != "pending_auditee":
        raise ValueError(f"Checkpoint {code} is not awaiting an auditee response (status: {resp.overallStatus})")

    now = _utcnow()
    resp.auditeeResponse = {
        "respondent_user_id": user.id,
        "response_text": payload.get("responseText", ""),
        "action_taken": payload.get("actionTaken", ""),
        "action_date": payload.get("actionDate"),
        "estimated_closure_date": payload.get("estimatedClosureDate"),
        "photos": payload.get("photos") or [],
        "responded_at": now.isoformat(),
        "status": "responded",
    }
    resp.overallStatus = "response_submitted"

    if audit.status in ("submitted_pending_response",):
        audit.status = "response_in_progress"
    await db.flush()
    return {"ok": True, "checkpointCode": code, "overallStatus": resp.overallStatus}


async def pm_review(db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    code = payload["checkpointCode"]
    decision = payload["decision"]  # accepted | rejected
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be 'accepted' or 'rejected'")
    resp = next((r for r in audit.responses if r.checkpointCode == code), None)
    if resp is None:
        raise ValueError(f"Checkpoint {code} not found")
    if resp.overallStatus != "response_submitted":
        raise ValueError(f"Checkpoint {code} has no submitted response to review (status: {resp.overallStatus})")

    now = _utcnow()
    resp.plantManagerReview = {
        "reviewer_user_id": user.id,
        "decision": decision,
        "comments": payload.get("comments", ""),
        "reviewed_at": now.isoformat(),
    }
    if decision == "accepted":
        resp.overallStatus = "response_accepted"
    else:
        resp.overallStatus = "pending_auditee"
        if resp.auditeeResponse:
            resp.auditeeResponse = {**resp.auditeeResponse, "status": "rejected"}

    audit.status = "under_review"
    audit.score = _compute_score(audit, audit.responses)
    await db.flush()
    return {"ok": True, "checkpointCode": code, "decision": decision}


async def close_audit(db: AsyncSession, *, user: User, audit_id: str, closing_remarks: str = "") -> dict[str, Any]:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status not in ("submitted_pending_response", "response_in_progress", "under_review"):
        raise ValueError(f"Audit cannot be closed from status '{audit.status}' — it must be submitted first")
    now = _utcnow()
    score = _compute_score(audit, audit.responses)
    audit.score = score
    audit.overallCompliancePct = score["overall_score_pct"]
    audit.auditPassed = score["audit_passed"]
    audit.criticalFailureCount = score["critical_failures"]
    audit.status = "closed"
    audit.actualEndAt = audit.actualEndAt or now
    audit.closedAt = now
    if closing_remarks:
        audit.closingRemarks = closing_remarks
    await db.flush()
    return {"ok": True, "status": "closed", "score": score}
