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

import hashlib
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.audit_compliance import (
    AuditCheckpointLibrary,
    AuditCheckpointResponse,
    AuditReport,
    AuditTemplate,
    CheckpointInteraction,
    ComplianceAudit,
)
from app.models.factory import FactoryProfile
from app.models.plant import Plant
from app.models.user import User

MINIMUM_PASS_SCORE = 80.0

# capa_severity_if_triggered (checkpoint) -> CAPA severity
_CAPA_SEVERITY = {"critical": "CRITICAL", "major": "HIGH", "minor": "MODERATE"}

# normalized scoring bucket -> first-class assessmentStatus (audit-lifecycle v2)
_ASSESS_STATUS = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL", "na": "NA"}

# overallStatus values for a checkpoint that has NOT yet been submitted to the
# auditee workflow — routing can be cleared freely on these.
_PRE_SUBMIT_STATUSES = {
    "not_answered", "answered_pass", "answered_partial", "answered_fail", "answered_na",
}

# CheckpointWorkflowState — the iteration state machine (audit-lifecycle v2).
# Terminal-for-finalization states; an audit can only close once EVERY
# checkpoint is in one of these.
_TERMINAL_STATES = {"PASSED", "RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED"}


async def _notify(db: AsyncSession, user_id: str | None, subject: str, body: str) -> None:
    """Best-effort handoff notification (email). Never raises — a notification
    failure must not block the transition (mirrors erm.notify_escalation). The
    my-checkpoints inbox is the primary in-app channel; this is the nudge."""
    if not user_id:
        return
    try:
        u = await db.get(User, user_id)
        email = getattr(u, "email", None) if u else None
        if not email:
            return
        from app.services.notifications import send_email
        await send_email([email], subject, body)
    except Exception:  # noqa: BLE001 — notifications are best-effort
        pass


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


def _response_to_dict(r: AuditCheckpointResponse, *, include_interactions: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
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
        "orderIndex": r.orderIndex,
        "requiresPhotoOnFail": r.requiresPhotoOnFail,
        "autoTriggerCapaOnFail": r.autoTriggerCapaOnFail,
        "capaSeverity": r.capaSeverity,
        "linkedSafeopsModule": r.linkedSafeopsModule,
        "routedToUserId": r.routedToUserId,
        # Ownership (audit-lifecycle v2).
        "assignedOwnerId": r.assignedOwnerId,
        "assignedById": r.assignedById,
        "assignedAt": _iso(r.assignedAt),
        # Ad-hoc / custom flag.
        "isAdHoc": r.isAdHoc,
        "addedById": r.addedById,
        # Two-axis state.
        "assessmentStatus": r.assessmentStatus,
        "workflowState": r.workflowState,
        "currentRound": r.currentRound,
        # Carousel capture.
        "observation": r.observation,
        "auditorNote": r.auditorNote,
        "auditorEvidenceIds": r.auditorEvidenceIds or [],
        "auditeeEvidenceIds": r.auditeeEvidenceIds or [],
        "capaId": r.capaId,
        "finalizedAt": _iso(r.finalizedAt),
        "auditorResponse": r.auditorResponse,
        "auditeeResponse": r.auditeeResponse,
        "plantManagerReview": r.plantManagerReview,
        "capa": r.capa,
        "overallStatus": r.overallStatus,
        "answeredAt": _iso(r.answeredAt),
    }
    if include_interactions:
        d["interactions"] = [
            _interaction_to_dict(i) for i in sorted(r.interactions, key=lambda x: (x.timestamp, x.round))
        ]
    return d


def _audit_to_dict(a: ComplianceAudit, *, include_responses: bool = False, include_interactions: bool = False) -> dict[str, Any]:
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
        "selectedDisciplineIds": a.selectedDisciplineIds or [],
        "scopePresetUsed": a.scopePresetUsed,
        "materializedCheckpointCount": a.materializedCheckpointCount,
        "adHocCount": a.adHocCount,
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
            _response_to_dict(r, include_interactions=include_interactions)
            for r in sorted(a.responses, key=lambda x: (x.categoryId, x.sequence))
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


async def _load_audit(
    db: AsyncSession, audit_id: str, *, with_responses: bool = False, with_interactions: bool = False
) -> ComplianceAudit | None:
    stmt = select(ComplianceAudit).where(ComplianceAudit.id == audit_id)
    if with_interactions:
        stmt = stmt.options(selectinload(ComplianceAudit.responses).selectinload(AuditCheckpointResponse.interactions))
    elif with_responses:
        stmt = stmt.options(selectinload(ComplianceAudit.responses))
    return (await db.execute(stmt)).scalar_one_or_none()


def _is_terminal(r: AuditCheckpointResponse) -> bool:
    """Terminal-for-finalization. PASSED is terminal only when the verdict
    agrees (defends against a workflowState↔assessmentStatus desync). Tolerant
    of legacy rows: an assessed pass/NA or a legacy accepted response counts."""
    if r.workflowState in ("RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED"):
        return True
    if r.workflowState == "PASSED" and r.assessmentStatus in ("PASS", "NA", "NOT_ASSESSED"):
        return True
    if r.workflowState == "OPEN" and r.assessmentStatus in ("PASS", "NA"):
        return True
    if r.overallStatus == "response_accepted":
        return True
    return False


def _finalizability(audit: ComplianceAudit) -> dict[str, Any]:
    """Whether every checkpoint is terminal; lists blockers otherwise."""
    blockers = [
        {
            "checkpointCode": r.checkpointCode,
            "categoryName": r.categoryName,
            "workflowState": r.workflowState,
            "assessmentStatus": r.assessmentStatus,
        }
        for r in sorted(audit.responses, key=lambda x: (x.categoryId, x.sequence))
        if not _is_terminal(r)
    ]
    total = len(audit.responses)
    # An audit can only be finalized after it has been submitted (the conduct →
    # submit → resolve → finalize lifecycle); an all-pass in-progress audit is
    # not yet finalizable — it must be submitted first.
    submitted = audit.status in ("submitted_pending_response", "response_in_progress", "under_review", "closed")
    return {
        "finalizable": submitted and total > 0 and not blockers,
        "submitted": submitted,
        "total": total,
        "terminal": total - len(blockers),
        "blockerCount": len(blockers),
        "blockers": blockers,
    }


async def get_audit(db: AsyncSession, audit_id: str) -> dict[str, Any] | None:
    audit = await _load_audit(db, audit_id, with_responses=True, with_interactions=True)
    if audit is None:
        return None
    d = _audit_to_dict(audit, include_responses=True, include_interactions=True)
    d["progress"] = _live_progress(audit.responses)
    d["finalizability"] = _finalizability(audit)

    # A-03 overview enrichment: factory name + profile link, template name/version,
    # standards in scope, owner count.
    plant = await db.get(Plant, audit.plantId)
    d["plantName"] = plant.name if plant else audit.plantId
    d["plantCode"] = plant.code if plant else None
    fp = (
        await db.execute(
            select(FactoryProfile.id).where(FactoryProfile.siteId == audit.plantId)
        )
    ).scalar_one_or_none()
    d["factoryProfileId"] = fp
    if audit.templateId:
        tmpl = await db.get(AuditTemplate, audit.templateId)
        d["templateName"] = tmpl.name if tmpl else None
        d["templateVersion"] = tmpl.version if tmpl else None
    else:
        d["templateName"] = None
        d["templateVersion"] = None
    d["standards"] = sorted({(r.standard or "").strip() for r in audit.responses if (r.standard or "").strip()})
    d["ownerCount"] = len({r.assignedOwnerId or r.routedToUserId for r in audit.responses if (r.assignedOwnerId or r.routedToUserId)})

    # Resolve names for EVERY referenced actor (incl. cross-plant ALL_PLANTS
    # users the plant-scoped /users picker can't return) so the meta strip,
    # iteration thread and owner chips always show a name, not "—".
    uid_set: set[str] = {audit.leadAuditorUserId, audit.plantManagerUserId}
    uid_set.update(audit.coAuditors or [])
    uid_set.update((a.get("userId") if isinstance(a, dict) else a) for a in (audit.auditees or []))
    for r in audit.responses:
        uid_set.update((r.assignedOwnerId, r.routedToUserId, r.addedById, r.assignedById))
        for i in r.interactions:
            uid_set.add(i.actorId)
    uid_set = {u for u in uid_set if u}
    d["userNames"] = {}
    if uid_set:
        rows = (await db.execute(select(User.id, User.name).where(User.id.in_(uid_set)))).all()
        d["userNames"] = {uid: nm for uid, nm in rows}
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
    template: AuditTemplate | None = None

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

    # Discipline scope (audit-lifecycle v2). A discipline is a library
    # category_code. When the client sends a non-empty selection it is
    # AUTHORITATIVE: the discipline chips define exactly which checkpoints
    # materialize (every checkpoint in each selected discipline), and any
    # template code/category subset is ignored — the chips already express the
    # scope and the live "will materialize N" count is the sum of the selected
    # disciplines. An empty selection means "full library" (back-compat for
    # programmatic callers), and in that path the template subset
    # codes/categories still filter as before.
    selected = set(data.get("selectedDisciplineIds") or [])

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
        scopePresetUsed=data.get("scopePresetUsed"),
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
    materialized_disciplines: list[str] = []
    seq = 0
    for cat in library.categories or []:
        cat_code = cat.get("category_code")
        if selected:
            if cat_code not in selected:
                continue
        elif mode == "subset" and subset_categories and cat_code not in subset_categories:
            continue
        order_in_disc = 0
        for cp in cat.get("checkpoints", []):
            code = cp.get("code")
            # Template code subset only applies on the back-compat (no explicit
            # discipline selection) path — see the `selected` comment above.
            if not selected and mode == "subset" and subset_codes and code not in subset_codes:
                continue
            seq += 1
            order_in_disc += 1
            owner = _route_for_category(cat_code, auditees)
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
                    orderIndex=order_in_disc,
                    requiresPhotoOnFail=bool(cp.get("requires_photo_on_fail", False)),
                    autoTriggerCapaOnFail=bool(cp.get("auto_trigger_capa_on_fail", False)),
                    capaSeverity=cp.get("capa_severity_if_triggered"),
                    linkedSafeopsModule=cp.get("linked_safeops_module"),
                    routedToUserId=owner,
                    assignedOwnerId=owner,
                    assessmentStatus="NOT_ASSESSED",
                    workflowState="OPEN",
                    currentRound=0,
                    isAdHoc=False,
                    overallStatus="not_answered",
                )
            )
        if order_in_disc:
            materialized_disciplines.append(cat_code)

    # Template custom checkpoints (audit-lifecycle v2): materialize the chosen
    # template's custom checkpoints whose discipline is in scope. Flagged custom
    # via isAdHoc; they continue each discipline's orderIndex.
    if template is not None and (template.customCheckpoints or []):
        order_max: dict[str, int] = {}
        meta: dict[str, tuple[str, str]] = {}
        for r in rows:
            order_max[r.categoryId] = max(order_max.get(r.categoryId, 0), r.orderIndex)
            meta[r.categoryId] = (r.categoryName, r.categoryColor)
        lib_cats = {c.get("category_code"): c for c in (library.categories or [])}
        for ccp in template.customCheckpoints or []:
            dcode = ccp.get("discipline_code")
            if not dcode:
                continue
            if selected and dcode not in selected:
                continue  # respect the discipline scope
            if dcode in meta:
                cname, ccolor = meta[dcode]
            else:
                libcat = lib_cats.get(dcode)
                cname = (libcat or {}).get("category_name") or ccp.get("discipline_name", "")
                ccolor = (libcat or {}).get("category_color", "")
            seq += 1
            order_max[dcode] = order_max.get(dcode, 0) + 1
            owner = _route_for_category(dcode, auditees)
            rows.append(
                _new_checkpoint_row(
                    audit=audit, cat_code=dcode, cat_name=cname, cat_color=ccolor,
                    code=ccp.get("code") or f"CUST-{dcode}-{order_max[dcode]:02d}",
                    question=ccp.get("question", ""), criticality=ccp.get("criticality", "major"),
                    guidance=ccp.get("guidance", ""),
                    requirement_reference=ccp.get("requirement_reference", ""),
                    standard=ccp.get("standard", ""),
                    requires_photo=bool(ccp.get("evidence_required_on_fail")),
                    sequence=seq, order_index=order_max[dcode], owner=owner,
                    is_adhoc=True, added_by=ccp.get("added_by_id"),
                )
            )
            if dcode not in materialized_disciplines:
                materialized_disciplines.append(dcode)

    # Guard: never persist a phantom empty audit. The session is rolled back on
    # this ValueError (router → 400), so nothing leaks.
    if not rows:
        raise ValueError(
            "The selected scope produced no checkpoints — adjust the disciplines or template."
        )

    db.add_all(rows)
    # Record the *actual* materialized scope (so an empty input resolves to the
    # full discipline list, and the audit self-describes its scope).
    audit.selectedDisciplineIds = materialized_disciplines
    audit.totalCheckpoints = len(rows)
    audit.materializedCheckpointCount = len(rows)
    audit.adHocCount = 0
    await db.flush()
    return audit


async def add_disciplines(
    db: AsyncSession, *, user: User, audit_id: str, discipline_ids: list[str]
) -> dict[str, Any]:
    """Materialize one or more additional disciplines into a running audit
    (before finalization), without disturbing existing checkpoints."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    # Pre-finalization only. After submit the score/compliance snapshot is frozen
    # and auto-CAPA has already run; adding checkpoints then would corrupt the
    # denominators and silently skip auto-CAPA on the new rows.
    if audit.status not in ("scheduled", "in_progress"):
        raise ValueError(
            f"Audit is '{audit.status}'; disciplines can only be added before submission"
        )

    library = (
        await db.execute(
            select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == audit.industryCode)
        )
    ).scalar_one_or_none()
    if library is None:
        raise ValueError(f"No checkpoint library for industry {audit.industryCode}")

    existing_codes = {r.checkpointCode for r in audit.responses}
    existing_disc = list(audit.selectedDisciplineIds or [])
    want = set(discipline_ids) - set(existing_disc)
    seq = max((r.sequence for r in audit.responses), default=0)
    auditees = audit.auditees or []

    new_rows: list[AuditCheckpointResponse] = []
    added_disc: list[str] = []
    for cat in library.categories or []:
        cat_code = cat.get("category_code")
        if cat_code not in want:
            continue
        order_in_disc = 0
        for cp in cat.get("checkpoints", []):
            code = cp.get("code")
            if code in existing_codes:
                continue  # never duplicate an already-materialized checkpoint
            seq += 1
            order_in_disc += 1
            owner = _route_for_category(cat_code, auditees)
            new_rows.append(
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
                    orderIndex=order_in_disc,
                    requiresPhotoOnFail=bool(cp.get("requires_photo_on_fail", False)),
                    autoTriggerCapaOnFail=bool(cp.get("auto_trigger_capa_on_fail", False)),
                    capaSeverity=cp.get("capa_severity_if_triggered"),
                    linkedSafeopsModule=cp.get("linked_safeops_module"),
                    routedToUserId=owner,
                    assignedOwnerId=owner,
                    assessmentStatus="NOT_ASSESSED",
                    workflowState="OPEN",
                    currentRound=0,
                    isAdHoc=False,
                    overallStatus="not_answered",
                )
            )
        if order_in_disc:
            added_disc.append(cat_code)

    db.add_all(new_rows)
    audit.selectedDisciplineIds = existing_disc + added_disc
    audit.totalCheckpoints = (audit.totalCheckpoints or 0) + len(new_rows)
    audit.materializedCheckpointCount = audit.totalCheckpoints
    await db.flush()
    return {
        "ok": True,
        "added": len(new_rows),
        "disciplines": added_disc,
        "totalCheckpoints": audit.totalCheckpoints,
    }


# ─────────────────────────────────────────────────────────────────────
# Custom checkpoints (audit-lifecycle v2) — ad-hoc to a live audit + template
# fork + promote-to-template. `isAdHoc` on an instance flags ANY custom
# (non-base-library) checkpoint so it shows the "Custom" badge everywhere;
# `adHocCount` on the audit counts ad-hoc additions made during conduct.
# ─────────────────────────────────────────────────────────────────────


def _new_checkpoint_row(
    *, audit: ComplianceAudit, cat_code: str, cat_name: str, cat_color: str, code: str,
    question: str, criticality: str, guidance: str = "", requirement_reference: str = "",
    standard: str = "", response_type: str = "pass_partial_fail", requires_photo: bool = False,
    auto_capa: bool = False, capa_severity: str | None = None, linked_module: str | None = None,
    sequence: int, order_index: int, owner: str | None = None, is_adhoc: bool = False,
    added_by: str | None = None,
) -> AuditCheckpointResponse:
    return AuditCheckpointResponse(
        auditId=audit.id, plantId=audit.plantId,
        checkpointCode=code, checkpointQuestion=question, guidance=guidance or "",
        requirementReference=requirement_reference or "", standard=standard or "",
        categoryId=cat_code, categoryName=cat_name, categoryColor=cat_color or "",
        criticality=criticality or "major", responseType=response_type or "pass_partial_fail",
        sequence=sequence, orderIndex=order_index,
        requiresPhotoOnFail=bool(requires_photo), autoTriggerCapaOnFail=bool(auto_capa),
        capaSeverity=capa_severity, linkedSafeopsModule=linked_module,
        routedToUserId=owner, assignedOwnerId=owner,
        assessmentStatus="NOT_ASSESSED", workflowState="OPEN", currentRound=0,
        isAdHoc=is_adhoc, addedById=added_by, overallStatus="not_answered",
    )


def _interaction_to_dict(i: CheckpointInteraction) -> dict[str, Any]:
    return {
        "id": i.id,
        "checkpointInstanceId": i.checkpointInstanceId,
        "auditId": i.auditId,
        "round": i.round,
        "actorId": i.actorId,
        "actorRole": i.actorRole,
        "action": i.action,
        "comment": i.comment,
        "evidenceIds": i.evidenceIds or [],
        "resultingState": i.resultingState,
        "timestamp": _iso(i.timestamp),
    }


async def _log_interaction(
    db: AsyncSession, *, instance: AuditCheckpointResponse, audit_id: str, actor_id: str,
    actor_role: str, action: str, resulting_state: str, comment: str | None = None,
    evidence_ids: list[str] | None = None, round: int | None = None, at: datetime | None = None,
) -> CheckpointInteraction:
    """Append one immutable row to a checkpoint's iteration thread. The Gate-6
    state machine reuses this; Gate 4 uses it only for ADHOC_ADDED. Pass `at` to
    stamp an explicit timestamp — used when several interactions are logged in
    one transaction (server now() would tie them) so the thread stays ordered."""
    inter = CheckpointInteraction(
        checkpointInstanceId=instance.id,
        auditId=audit_id,
        round=round if round is not None else instance.currentRound,
        actorId=actor_id,
        actorRole=actor_role,
        action=action,
        comment=comment,
        evidenceIds=evidence_ids or [],
        resultingState=resulting_state,
    )
    # Always stamp from ONE clock (the app's), never the DB server_default — so
    # ordering is consistent across actions and a single transaction's multiple
    # logs (which DB now() would tie) stay deterministically ordered via `at`.
    inter.timestamp = at if at is not None else _utcnow()
    db.add(inter)
    return inter


def _actor_role_for(user: User, audit: ComplianceAudit) -> str:
    return "LEAD_AUDITOR" if user.id == audit.leadAuditorUserId else "AUDITOR"


async def add_adhoc_checkpoint(
    db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Auditor adds a custom checkpoint to THIS audit only (carousel "+").
    Slots into its discipline, counts toward scoring, logs ADHOC_ADDED, and
    optionally promotes itself to the audit's template."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status not in ("scheduled", "in_progress"):
        raise ValueError(f"Audit is '{audit.status}'; checkpoints can only be added before submission")
    if payload.get("promoteToTemplate") and not audit.templateId:
        raise ValueError("This audit has no template to promote the checkpoint to")

    disc_code = payload.get("disciplineId") or payload.get("disciplineCode")
    if not disc_code:
        raise ValueError("disciplineId is required")
    question = (payload.get("question") or "").strip()
    if len(question) < 4:
        raise ValueError("question must be at least 4 characters")

    # Resolve the discipline's display name/colour from existing rows, else the library.
    name: str | None = None
    color = ""
    for r in audit.responses:
        if r.categoryId == disc_code:
            name, color = r.categoryName, r.categoryColor
            break
    if name is None:
        library = (
            await db.execute(
                select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == audit.industryCode)
            )
        ).scalar_one_or_none()
        libcat = next(
            (c for c in ((library.categories if library else []) or []) if c.get("category_code") == disc_code),
            None,
        )
        if libcat is None:
            raise ValueError(f"Unknown discipline '{disc_code}'")
        name, color = libcat.get("category_name", ""), libcat.get("category_color", "")

    existing_codes = {r.checkpointCode for r in audit.responses}
    n_adhoc = (audit.adHocCount or 0) + 1
    code = f"{audit.auditNumber}-AH{n_adhoc:02d}"
    while code in existing_codes:
        n_adhoc += 1
        code = f"{audit.auditNumber}-AH{n_adhoc:02d}"

    order_index = max((r.orderIndex for r in audit.responses if r.categoryId == disc_code), default=0) + 1
    seq = max((r.sequence for r in audit.responses), default=0) + 1
    owner = payload.get("assignedOwnerId") or _route_for_category(disc_code, audit.auditees or [])

    row = _new_checkpoint_row(
        audit=audit, cat_code=disc_code, cat_name=name, cat_color=color, code=code, question=question,
        criticality=payload.get("severity") or payload.get("criticality") or "major",
        guidance=payload.get("guidance", ""),
        requirement_reference=payload.get("requirementReference", ""),
        standard=payload.get("standardClauseRef") or payload.get("standard", ""),
        requires_photo=bool(payload.get("evidenceRequiredOnFail")),
        sequence=seq, order_index=order_index, owner=owner, is_adhoc=True, added_by=user.id,
    )
    db.add(row)
    await db.flush()

    await _log_interaction(
        db, instance=row, audit_id=audit.id, actor_id=user.id, actor_role=_actor_role_for(user, audit),
        action="ADHOC_ADDED", resulting_state="OPEN", round=0,
        comment=f"Ad-hoc checkpoint added to {name}",
    )

    if disc_code not in (audit.selectedDisciplineIds or []):
        audit.selectedDisciplineIds = list(audit.selectedDisciplineIds or []) + [disc_code]
    audit.adHocCount = (audit.adHocCount or 0) + 1
    audit.totalCheckpoints = (audit.totalCheckpoints or 0) + 1
    audit.materializedCheckpointCount = (audit.materializedCheckpointCount or 0) + 1

    promoted_id: str | None = None
    if payload.get("promoteToTemplate") and audit.templateId:
        fork = await _fork_template_with_checkpoint(
            db, user=user, template_id=audit.templateId,
            cp={
                "discipline_code": disc_code, "discipline_name": name, "question": question,
                "criticality": row.criticality, "guidance": row.guidance,
                "requirement_reference": row.requirementReference, "standard": row.standard,
                "evidence_required_on_fail": row.requiresPhotoOnFail,
            },
        )
        promoted_id = fork.id

    await db.flush()
    return {"ok": True, "checkpoint": _response_to_dict(row), "promotedTemplateId": promoted_id}


async def _fork_template_with_checkpoint(
    db: AsyncSession, *, user: User, template_id: str, cp: dict[str, Any]
) -> AuditTemplate:
    """Fork a new template version with one custom checkpoint appended; retire
    the parent (templates are versioned/immutable once forked).

    The parent row is SELECT … FOR UPDATE locked so two concurrent forks of the
    same template serialize: the first retires it, the second re-reads it as
    inactive and is rejected (rather than both forking from the same baseline
    into divergent sibling versions). Only the active head can be forked."""
    parent = (
        await db.execute(
            select(AuditTemplate).where(AuditTemplate.id == template_id).with_for_update()
        )
    ).scalar_one_or_none()
    if parent is None:
        raise ValueError("Template not found")
    if not parent.isActive:
        raise ValueError("This template version has been superseded; fork the current version instead")

    existing = list(parent.customCheckpoints or [])
    dcode = cp.get("discipline_code") or "GEN"
    code = cp.get("code") or f"CUST-{dcode}-{len(existing) + 1:02d}"
    cp_def = {
        "code": code,
        "discipline_code": dcode,
        "discipline_name": cp.get("discipline_name", ""),
        "question": cp.get("question", ""),
        "criticality": cp.get("criticality", "major"),
        "guidance": cp.get("guidance", ""),
        "requirement_reference": cp.get("requirement_reference", ""),
        "standard": cp.get("standard", ""),
        "evidence_required_on_fail": bool(cp.get("evidence_required_on_fail")),
        "is_custom": True,
        "added_by_id": user.id,
        "added_at": _utcnow().isoformat(),
    }
    try:
        new_version = f"{int(float(parent.version)) + 1}.0"
    except (TypeError, ValueError):
        new_version = f"{parent.version}-v2"

    fork = AuditTemplate(
        tenantId=parent.tenantId, name=parent.name, description=parent.description,
        auditType=parent.auditType, baseIndustry=parent.baseIndustry,
        checkpointConfiguration=parent.checkpointConfiguration,
        customCheckpoints=existing + [cp_def], parentTemplateId=parent.id,
        scoring=parent.scoring, workflow=parent.workflow, isActive=True,
        version=new_version, createdByUserId=user.id,
    )
    db.add(fork)
    parent.isActive = False  # retire the parent version; keep it for history
    await db.flush()
    return fork


async def allocate_checkpoints(
    db: AsyncSession, *, user: User, audit_id: str, owner_id: str | None,
    checkpoint_ids: list[str] | None = None, discipline_id: str | None = None,
) -> dict[str, Any]:
    """Plant Head / Lead Auditor allocates checkpoints to an owner — per-row,
    bulk (checkpoint_ids), or whole-discipline (discipline_id). owner_id=None
    unassigns. Sets assignedOwnerId + keeps routedToUserId in sync; each change
    logs a ROUTED_TO_OWNER interaction (reassignment carries any in-flight
    iteration with it)."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; allocation is locked")

    ids = set(checkpoint_ids or [])
    targets = [
        r for r in audit.responses
        if (r.id in ids) or (discipline_id is not None and r.categoryId == discipline_id)
    ]
    if not targets:
        raise ValueError("No matching checkpoints to allocate")

    owner_name = owner_id
    if owner_id:
        u = await db.get(User, owner_id)
        if u is None:
            raise ValueError("Owner not found")
        if u.plantId and u.plantId != audit.plantId:
            raise ValueError("Owner belongs to a different plant")
        owner_name = u.name

    # Default responder for an in-flight finding that is being unassigned — so
    # it never drops out of every inbox (it would otherwise become un-routable).
    default_owner = audit.plantManagerUserId or audit.leadAuditorUserId

    now = _utcnow()
    updated = 0
    for r in targets:
        if r.assignedOwnerId == owner_id:
            continue
        prev = r.assignedOwnerId
        r.assignedOwnerId = owner_id
        r.assignedById = user.id
        r.assignedAt = now
        if owner_id:
            r.routedToUserId = owner_id  # assignment routes the finding here
        elif r.overallStatus in _PRE_SUBMIT_STATUSES:
            r.routedToUserId = None  # not yet submitted — safe to clear
        else:
            # In-flight (pending_auditee / response_submitted / …): keep it
            # routed to a real responder rather than orphaning it.
            r.routedToUserId = default_owner
        comment = (
            "Unassigned" if not owner_id
            else f"Reassigned to {owner_name}" if prev
            else f"Assigned to {owner_name}"
        )
        await _log_interaction(
            db, instance=r, audit_id=audit.id, actor_id=user.id,
            actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
            resulting_state=r.workflowState, comment=comment,
        )
        updated += 1

    await db.flush()
    return {"ok": True, "updated": updated, "ownerId": owner_id}


async def my_assigned_checkpoints(
    db: AsyncSession, *, user: User, accessible_plants: list[str] | None = None
) -> dict[str, Any]:
    """Auditee transparency (A-06): every checkpoint assigned to me across all
    audits, in every state, grouped by audit with a personal scorecard. Scoped
    to the caller's accessible plants (mirrors list_audits)."""
    stmt = (
        select(AuditCheckpointResponse, ComplianceAudit)
        .join(ComplianceAudit, AuditCheckpointResponse.auditId == ComplianceAudit.id)
        .where(
            or_(
                AuditCheckpointResponse.routedToUserId == user.id,
                AuditCheckpointResponse.assignedOwnerId == user.id,
            )
        )
        .order_by(ComplianceAudit.scheduledDate.desc())
    )
    if accessible_plants is not None:
        stmt = stmt.where(ComplianceAudit.plantId.in_(accessible_plants))
    rows = (await db.execute(stmt)).all()

    audits_map: dict[str, dict[str, Any]] = {}
    totals = {"total": 0, "needsResponse": 0, "audits": 0}
    for r, a in rows:
        grp = audits_map.get(a.id)
        if grp is None:
            grp = {
                "auditId": a.id, "auditNumber": a.auditNumber, "title": a.title,
                "status": a.status, "plantId": a.plantId, "industryCode": a.industryCode,
                "items": [],
                "scorecard": {"total": 0, "pass": 0, "partial": 0, "fail": 0, "na": 0,
                              "not_assessed": 0, "needsResponse": 0},
            }
            audits_map[a.id] = grp
        needs = r.overallStatus == "pending_auditee"
        d = _response_to_dict(r)
        d["needsResponse"] = needs
        grp["items"].append(d)
        sc = grp["scorecard"]
        sc["total"] += 1
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        sc[val if val in ("pass", "partial", "fail", "na") else "not_assessed"] += 1
        if needs:
            sc["needsResponse"] += 1
            totals["needsResponse"] += 1
        totals["total"] += 1

    audits = list(audits_map.values())
    totals["audits"] = len(audits)
    return {"audits": audits, "totals": totals}


async def add_template_custom_checkpoint(
    db: AsyncSession, *, user: User, template_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Template-level surface (A-08a): add a custom checkpoint to a discipline of
    a template, forking a new version that future audits pick up as standard."""
    disc_code = payload.get("disciplineId") or payload.get("disciplineCode")
    if not disc_code:
        raise ValueError("disciplineId is required")
    question = (payload.get("question") or "").strip()
    if len(question) < 4:
        raise ValueError("question must be at least 4 characters")
    fork = await _fork_template_with_checkpoint(
        db, user=user, template_id=template_id,
        cp={
            "discipline_code": disc_code,
            "discipline_name": payload.get("disciplineName", ""),
            "question": question,
            "criticality": payload.get("severity") or payload.get("criticality") or "major",
            "guidance": payload.get("guidance", ""),
            "requirement_reference": payload.get("requirementReference", ""),
            "standard": payload.get("standardClauseRef") or payload.get("standard", ""),
            "evidence_required_on_fail": bool(payload.get("evidenceRequiredOnFail")),
        },
    )
    return {
        "ok": True,
        "templateId": fork.id,
        "version": fork.version,
        "parentTemplateId": fork.parentTemplateId,
        "customCheckpointCount": len(fork.customCheckpoints or []),
    }


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

    # Mirror the auditor's verdict into the first-class carousel fields so
    # reports + the iteration thread read structured columns rather than
    # re-parsing the auditorResponse JSON blob.
    resp.assessmentStatus = _ASSESS_STATUS.get(val, "NOT_ASSESSED")
    if "text_observation" in merged:
        resp.observation = merged.get("text_observation") or None
    if "photos" in merged:
        resp.auditorEvidenceIds = [
            p.get("storagePath") for p in (merged.get("photos") or []) if isinstance(p, dict) and p.get("storagePath")
        ]

    # Reconcile the iteration state with the (possibly edited) verdict. The
    # auditor's verdict steers OPEN/PASSED checkpoints AND in-flight findings
    # (AWAITING_AUDITEE/AUDITEE_RESPONDED/MORE_INFO_REQUESTED) — so a re-assess
    # after REOPEN works, a late fail enters the thread, and re-passing an
    # in-flight finding closes it out. ESCALATED_PM and the resolved terminals
    # (RESOLVED/ACCEPTED_WITH_CAPA/FINALIZED) are owned by the PM / state machine
    # and are never overridden by a carousel save.
    post_submit = audit.status in ("submitted_pending_response", "response_in_progress", "under_review")
    _IN_FLIGHT = ("AWAITING_AUDITEE", "AUDITEE_RESPONDED", "MORE_INFO_REQUESTED")
    if resp.workflowState in ("OPEN", "PASSED", *_IN_FLIGHT):
        if val in ("pass", "na"):
            if resp.workflowState in _IN_FLIGHT:
                # Re-assessed as compliant — close the in-flight finding.
                resp.routedToUserId = None
                resp.currentRound = 0
                await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                                       actor_role=_actor_role_for(user, audit), action="AUDITOR_ACCEPT",
                                       resulting_state="PASSED", round=resp.currentRound,
                                       comment="Re-assessed as compliant — finding closed.", at=now)
            resp.workflowState = "PASSED"
        elif val in ("fail", "partial"):
            if resp.workflowState in ("OPEN", "PASSED"):
                if post_submit:
                    # Post-submit reassess routes a finding straight into the
                    # thread with no second submit gate — so enforce the SAME
                    # evidence rule submit_audit applies (an observation, plus a
                    # photo where the checkpoint demands one). Without this the
                    # reopen→fail path would mint an evidence-free finding/CAPA.
                    if not (resp.observation or "").strip():
                        raise ValueError("An observation is required before routing a fail/partial finding.")
                    if resp.requiresPhotoOnFail and not (resp.auditorEvidenceIds or []):
                        raise ValueError("An evidence photo is required for this checkpoint before routing the finding.")
                    owner = resp.assignedOwnerId or resp.routedToUserId or audit.plantManagerUserId or audit.leadAuditorUserId
                    resp.routedToUserId = owner
                    resp.workflowState = "AWAITING_AUDITEE"
                    resp.overallStatus = "pending_auditee"
                    resp.currentRound = 0
                    await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                                           actor_role=_actor_role_for(user, audit), action="ASSESSED",
                                           resulting_state="AWAITING_AUDITEE", round=0,
                                           comment=(resp.observation or "")[:500] or None, at=now)
                    await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                                           actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
                                           resulting_state="AWAITING_AUDITEE", round=0, at=now + timedelta(milliseconds=1))
                    await _notify(db, owner, f"Audit {audit.auditNumber}: finding assigned to you",
                                  f"Checkpoint {code} needs your response.")
                else:
                    resp.workflowState = "OPEN"  # pre-submit — submit_audit will route it
            # already in-flight and still fail/partial → leave the thread untouched
        elif resp.workflowState in ("OPEN", "PASSED"):  # verdict cleared
            resp.workflowState = "OPEN"

    # Coherence guard: the unconditional overallStatus rewrite above reflects the
    # raw verdict (answered_fail, …) — but an in-flight finding's overallStatus is
    # owned by the auditee workflow, not the verdict. Re-snap it so a verdict edit
    # on a routed finding can never strand it out of pending_auditee /
    # response_submitted (which would break auditee_respond + the needs-response
    # inbox count). Re-pass already moved it to PASSED, so it's excluded here.
    if resp.workflowState in ("AWAITING_AUDITEE", "MORE_INFO_REQUESTED"):
        resp.overallStatus = "pending_auditee"
    elif resp.workflowState == "AUDITEE_RESPONDED":
        resp.overallStatus = "response_submitted"

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

    # Enforce the "observation/evidence required on fail/partial" rule the
    # carousel shows — every fail/partial needs an observation, and a photo
    # where the checkpoint demands it. Otherwise the finding (and any auto-CAPA)
    # carries no substance.
    missing: list[str] = []
    for r in audit.responses:
        ar = r.auditorResponse or {}
        v = _norm_value(ar.get("value"))
        if v in ("fail", "partial"):
            if not (ar.get("text_observation") or "").strip():
                missing.append(f"{r.checkpointCode} (observation)")
            elif r.requiresPhotoOnFail and not (ar.get("photos") or []):
                missing.append(f"{r.checkpointCode} (evidence photo)")
    if missing:
        head = ", ".join(missing[:8])
        more = f" + {len(missing) - 8} more" if len(missing) > 8 else ""
        raise ValueError(f"{len(missing)} fail/partial checkpoint(s) need an observation/evidence before submit: {head}{more}")

    now = _utcnow()
    capa_count = 0
    routed_owners: set[str] = set()
    for r in audit.responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        if val in ("fail", "partial"):
            r.overallStatus = "pending_auditee"
            # Route to the allocated owner; an unassigned finding routes to a
            # default (plant manager / lead) but leaves assignedOwnerId null so
            # the allocation UI still flags it "unassigned".
            owner = r.assignedOwnerId or r.routedToUserId or audit.plantManagerUserId or audit.leadAuditorUserId
            r.routedToUserId = owner
            # Open the iteration thread: the auditor's finding, then the route.
            # Distinct timestamps keep ASSESSED strictly before ROUTED_TO_OWNER
            # (both are logged in this one transaction, so server now() ties).
            r.workflowState = "AWAITING_AUDITEE"
            r.currentRound = 0
            t = _utcnow()
            await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                                   actor_role=_actor_role_for(user, audit), action="ASSESSED",
                                   resulting_state="AWAITING_AUDITEE", round=0,
                                   comment=(r.observation or "")[:500] or None, at=t)
            await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                                   actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
                                   resulting_state="AWAITING_AUDITEE", round=0, at=t + timedelta(milliseconds=1))
            if owner:
                routed_owners.add(owner)
        elif val in ("pass", "na"):
            r.workflowState = "PASSED"
        if val == "fail" and r.autoTriggerCapaOnFail:
            spawned = await _spawn_capa(db, user=user, audit=audit, response=r)
            if spawned:
                r.workflowState = "ACCEPTED_WITH_CAPA"
                capa_count += 1

    for owner_id in routed_owners:
        await _notify(db, owner_id, f"Audit {audit.auditNumber}: findings assigned to you",
                      f"Audit '{audit.title}' was submitted. Findings routed to you await your response.")

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
    if resp.workflowState not in ("AWAITING_AUDITEE", "MORE_INFO_REQUESTED"):
        raise ValueError(f"Checkpoint {code} is not awaiting a response (state: {resp.workflowState})")
    # SoD: only the routed owner may respond (same guard as the transition path).
    owner_ids = {resp.assignedOwnerId, resp.routedToUserId} - {None}
    if owner_ids and user.id not in owner_ids:
        raise ValueError("This checkpoint is routed to a different owner")
    if len((payload.get("actionTaken") or payload.get("responseText") or "").strip()) < 3:
        raise ValueError("Describe the action taken (at least 3 characters)")

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
        "round": resp.currentRound,
    }
    resp.overallStatus = "response_submitted"
    resp.workflowState = "AUDITEE_RESPONDED"  # keep the state machine in sync
    await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                           actor_role="AUDITEE", action="AUDITEE_RESPONSE", resulting_state="AUDITEE_RESPONDED",
                           comment=(payload.get("actionTaken") or payload.get("responseText") or "")[:500] or None,
                           round=resp.currentRound)
    await _notify(db, audit.leadAuditorUserId, f"Audit {audit.auditNumber}: response submitted",
                  f"Checkpoint {code} has an auditee response awaiting review.")

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
    # Legacy PM review only acts on an escalated checkpoint — the auditor reviews
    # first (AUDITEE_RESPONDED) and may escalate; the PM never resolves a
    # non-escalated finding (that would bypass the auditor's review).
    if resp.workflowState != "ESCALATED_PM":
        raise ValueError(f"Checkpoint {code} is not escalated for plant-manager decision (state: {resp.workflowState})")
    # SoD: the PM can't decide on a response they themselves authored.
    if (resp.auditeeResponse or {}).get("respondent_user_id") == user.id:
        raise ValueError("You can't decide on your own auditee response")

    now = _utcnow()
    resp.plantManagerReview = {
        "reviewer_user_id": user.id,
        "decision": decision,
        "comments": payload.get("comments", ""),
        "reviewed_at": now.isoformat(),
    }
    if decision == "accepted":
        resp.overallStatus = "response_accepted"
        resp.workflowState = "RESOLVED"
        await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                               actor_role="PLANT_MANAGER", action="PM_DECISION", resulting_state="RESOLVED",
                               comment=payload.get("comments") or None, round=resp.currentRound)
    else:
        resp.overallStatus = "pending_auditee"
        resp.currentRound += 1
        resp.workflowState = "MORE_INFO_REQUESTED"
        if resp.auditeeResponse:
            resp.auditeeResponse = {**resp.auditeeResponse, "status": "rejected"}
        await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                               actor_role="PLANT_MANAGER", action="PM_DECISION", resulting_state="MORE_INFO_REQUESTED",
                               comment=payload.get("comments") or None, round=resp.currentRound)
        await _notify(db, resp.routedToUserId, f"Audit {audit.auditNumber}: more information requested",
                      f"Checkpoint {code} was sent back — round {resp.currentRound}.")

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

    # Finalization gate (audit-lifecycle v2): every checkpoint must be terminal.
    fin = _finalizability(audit)
    if not fin["finalizable"]:
        raise ValueError(
            f"{fin['blockerCount']} checkpoint(s) still in review — resolve every checkpoint before closing"
        )

    now = _utcnow()
    score = _compute_score(audit, audit.responses)
    audit.score = score
    audit.overallCompliancePct = score["overall_score_pct"]
    audit.auditPassed = score["audit_passed"]
    audit.criticalFailureCount = score["critical_failures"]
    # Lock every checkpoint into FINALIZED.
    for r in audit.responses:
        r.workflowState = "FINALIZED"
        r.finalizedAt = now
    audit.status = "closed"
    audit.actualEndAt = audit.actualEndAt or now
    audit.closedAt = now
    if closing_remarks:
        audit.closingRemarks = closing_remarks
    await db.flush()
    return {"ok": True, "status": "closed", "score": score}


# ─────────────────────────────────────────────────────────────────────
# Iteration state machine (A-05) — multi-round auditor ↔ auditee ↔ PM.
# ─────────────────────────────────────────────────────────────────────

# action -> states it is valid from (server-side guard; the router also
# permission-gates by role).
_ACTION_FROM = {
    "AUDITEE_RESPOND": {"AWAITING_AUDITEE", "MORE_INFO_REQUESTED"},
    "ACCEPT": {"AUDITEE_RESPONDED"},
    "REQUEST_MORE_INFO": {"AUDITEE_RESPONDED"},
    "RAISE_CAPA": {"AUDITEE_RESPONDED"},
    "ESCALATE": {"AUDITEE_RESPONDED"},
    "PM_ACCEPT": {"ESCALATED_PM"},
    "PM_RAISE_CAPA": {"ESCALATED_PM"},
    "PM_SEND_BACK": {"ESCALATED_PM"},
    "REOPEN": {"PASSED"},
}


async def transition_checkpoint(
    db: AsyncSession, *, user: User, audit_id: str, checkpoint_id: str, action: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """Single state-machine dispatcher for the iteration thread. Validates the
    current workflowState allows `action`, performs it, appends an immutable
    interaction, increments the round on every send-back, spawns AUDIT-source
    CAPA on RAISE_CAPA, and fires best-effort handoff notifications."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; checkpoint actions are locked")

    r = next((x for x in audit.responses if x.id == checkpoint_id or x.checkpointCode == checkpoint_id), None)
    if r is None:
        raise ValueError("Checkpoint not found on this audit")

    valid_from = _ACTION_FROM.get(action)
    if valid_from is None:
        raise ValueError(f"Unknown action '{action}'")
    if r.workflowState not in valid_from:
        raise ValueError(f"Action '{action}' is not allowed from state '{r.workflowState}'")

    # Segregation of duties. The auditee response must come from the routed
    # owner (when one exists); the auditor review must not be done by the same
    # person who wrote the response being reviewed.
    if action == "AUDITEE_RESPOND":
        owner_ids = {r.assignedOwnerId, r.routedToUserId} - {None}
        if owner_ids and user.id not in owner_ids:
            raise ValueError("This checkpoint is routed to a different owner")
    if action in ("ACCEPT", "REQUEST_MORE_INFO", "RAISE_CAPA", "ESCALATE",
                  "PM_ACCEPT", "PM_RAISE_CAPA", "PM_SEND_BACK"):
        responder = (r.auditeeResponse or {}).get("respondent_user_id")
        if responder and responder == user.id:
            raise ValueError("You can't review your own auditee response")

    # Min-length parity with the client forms (server is the real gate).
    if action == "AUDITEE_RESPOND" and len((payload.get("actionTaken") or payload.get("comment") or "").strip()) < 3:
        raise ValueError("Describe the action taken (at least 3 characters)")
    if action in ("REQUEST_MORE_INFO", "PM_SEND_BACK") and len((payload.get("comment") or "").strip()) < 3:
        raise ValueError("A note (at least 3 characters) is required")

    now = _utcnow()
    comment = (payload.get("comment") or "").strip() or None
    evidence_ids = payload.get("evidenceIds") or []
    photos = payload.get("photos") or []

    if action == "AUDITEE_RESPOND":
        r.auditeeResponse = {
            "respondent_user_id": user.id,
            "response_text": comment or "",
            "action_taken": payload.get("actionTaken") or comment or "",
            "action_date": payload.get("actionDate"),
            "estimated_closure_date": payload.get("estimatedClosureDate"),
            "photos": photos,
            "responded_at": now.isoformat(),
            "status": "responded",
            "round": r.currentRound,
        }
        if evidence_ids:
            r.auditeeEvidenceIds = list(dict.fromkeys((r.auditeeEvidenceIds or []) + evidence_ids))
        r.workflowState = "AUDITEE_RESPONDED"
        r.overallStatus = "response_submitted"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role="AUDITEE",
                               action="AUDITEE_RESPONSE", resulting_state="AUDITEE_RESPONDED",
                               comment=comment, evidence_ids=evidence_ids, round=r.currentRound)
        await _notify(db, audit.leadAuditorUserId, f"Audit {audit.auditNumber}: response submitted",
                      f"Checkpoint {r.checkpointCode} awaits your review.")

    elif action == "ACCEPT":
        r.workflowState = "RESOLVED"
        r.overallStatus = "response_accepted"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="AUDITOR_ACCEPT",
                               resulting_state="RESOLVED", comment=comment, round=r.currentRound)

    elif action == "REQUEST_MORE_INFO":
        r.currentRound += 1
        r.workflowState = "MORE_INFO_REQUESTED"
        r.overallStatus = "pending_auditee"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="REQUEST_MORE_INFO",
                               resulting_state="MORE_INFO_REQUESTED", comment=comment, round=r.currentRound)
        await _notify(db, r.routedToUserId, f"Audit {audit.auditNumber}: more information requested",
                      f"Checkpoint {r.checkpointCode} needs more information — round {r.currentRound}.")

    elif action in ("RAISE_CAPA", "PM_RAISE_CAPA"):
        spawned = await _spawn_capa(db, user=user, audit=audit, response=r)
        if not spawned:
            # Never mint a CAPA-less ACCEPTED_WITH_CAPA terminal — fail the action
            # so it can be retried (mirrors submit_audit's `if spawned` guard).
            raise ValueError("Could not raise a CAPA for this checkpoint — please retry")
        r.capaId = (r.capa or {}).get("capa_id")
        r.workflowState = "ACCEPTED_WITH_CAPA"
        r.overallStatus = "response_accepted"
        if action == "PM_RAISE_CAPA":
            r.plantManagerReview = {"reviewer_user_id": user.id, "decision": "capa",
                                    "comments": comment or "", "reviewed_at": now.isoformat()}
            role, act = "PLANT_MANAGER", "PM_DECISION"
        else:
            role, act = _actor_role_for(user, audit), "RAISE_CAPA"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role=role,
                               action=act, resulting_state="ACCEPTED_WITH_CAPA",
                               comment=comment or (f"CAPA {r.capaId}" if r.capaId else None), round=r.currentRound)

    elif action == "ESCALATE":
        r.workflowState = "ESCALATED_PM"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="ESCALATE_PM",
                               resulting_state="ESCALATED_PM", comment=comment, round=r.currentRound)
        await _notify(db, audit.plantManagerUserId, f"Audit {audit.auditNumber}: checkpoint escalated",
                      f"Checkpoint {r.checkpointCode} was escalated for your decision.")

    elif action == "PM_ACCEPT":
        r.workflowState = "RESOLVED"
        r.overallStatus = "response_accepted"
        r.plantManagerReview = {"reviewer_user_id": user.id, "decision": "accepted",
                                "comments": comment or "", "reviewed_at": now.isoformat()}
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role="PLANT_MANAGER",
                               action="PM_DECISION", resulting_state="RESOLVED", comment=comment, round=r.currentRound)

    elif action == "PM_SEND_BACK":
        r.currentRound += 1
        r.workflowState = "MORE_INFO_REQUESTED"
        r.overallStatus = "pending_auditee"
        r.plantManagerReview = {"reviewer_user_id": user.id, "decision": "send_back",
                                "comments": comment or "", "reviewed_at": now.isoformat()}
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role="PLANT_MANAGER",
                               action="PM_DECISION", resulting_state="MORE_INFO_REQUESTED",
                               comment=comment, round=r.currentRound)
        await _notify(db, r.routedToUserId, f"Audit {audit.auditNumber}: sent back",
                      f"Checkpoint {r.checkpointCode} was sent back for more work — round {r.currentRound}.")

    elif action == "REOPEN":
        if not comment:
            raise ValueError("A reason is required to reopen a passed checkpoint")
        r.workflowState = "OPEN"
        r.finalizedAt = None
        # Reset the verdict so the reopened checkpoint is non-terminal and the
        # finalization gate actually blocks until it is re-assessed.
        r.assessmentStatus = "NOT_ASSESSED"
        r.overallStatus = "not_answered"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="REOPEN",
                               resulting_state="OPEN", comment=comment, round=r.currentRound)

    audit.score = _compute_score(audit, audit.responses)
    await db.flush()
    return {
        "ok": True,
        "checkpointCode": r.checkpointCode,
        "workflowState": r.workflowState,
        "currentRound": r.currentRound,
        "overallStatus": r.overallStatus,
    }


# ─────────────────────────────────────────────────────────────────────
# Reports (A-07) — Interim + Final, immutable snapshots.
# ─────────────────────────────────────────────────────────────────────


def _canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _result_label(score: dict[str, Any]) -> str:
    if score["critical_failures"] > 0:
        return "CRITICAL_NC"
    if score["major_failures"] > 0:
        return "MAJOR_NC"
    if score["minor_failures"] > 0 or score["partially_passed"] > 0:
        return "MINOR_NC"
    return "CONFORMING"


def _standards_rollup(responses: list[AuditCheckpointResponse]) -> list[dict[str, Any]]:
    """Aggregate conformance by standard (SA8000 / ISO 45001 / …) for the final."""
    agg: dict[str, dict[str, int]] = {}
    for r in responses:
        std = (r.standard or "").strip()
        if not std:
            continue
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        a = agg.setdefault(std, {"total": 0, "pass": 0, "partial": 0, "fail": 0, "na": 0})
        a["total"] += 1
        if val in ("pass", "partial", "fail", "na"):
            a[val] += 1
    out = []
    for std, a in sorted(agg.items()):
        assessable = a["pass"] + a["partial"] + a["fail"]
        a["scorePct"] = round((a["pass"] + 0.5 * a["partial"]) / assessable * 100, 1) if assessable else 0.0
        out.append({"standard": std, **a})
    return out


def _build_report_snapshot(audit: ComplianceAudit, report_type: str) -> dict[str, Any]:
    score = _compute_score(audit, audit.responses)
    responses = sorted(audit.responses, key=lambda x: (x.categoryId, x.sequence))
    total = len(responses)
    assessed = sum(1 for r in responses if r.assessmentStatus != "NOT_ASSESSED")

    findings: list[dict[str, Any]] = []
    open_iters: list[dict[str, Any]] = []
    crit_open = 0
    capa_total = capa_open = capa_overdue = 0
    register: list[dict[str, Any]] = []
    now = _naive(_utcnow())

    for r in responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        owner = r.assignedOwnerId or r.routedToUserId
        capa = r.capa or {}
        if capa.get("capa_id"):
            capa_total += 1
            st = capa.get("capa_status")
            if st not in ("CLOSED", "CLOSED_RECURRED", "VERIFIED"):
                capa_open += 1
            due = capa.get("capa_due_date")
            try:
                if st not in ("CLOSED", "CLOSED_RECURRED", "VERIFIED") and due and _naive(datetime.fromisoformat(due)) < now:
                    capa_overdue += 1
            except (TypeError, ValueError):
                pass
        if val in ("fail", "partial"):
            findings.append({
                "checkpointCode": r.checkpointCode, "discipline": r.categoryName, "severity": r.criticality,
                "assessmentStatus": r.assessmentStatus, "workflowState": r.workflowState, "round": r.currentRound,
                "ownerId": owner, "question": r.checkpointQuestion, "observation": r.observation,
                "standard": r.standard, "requirementReference": r.requirementReference,
                "capaNumber": capa.get("capa_number"), "capaStatus": capa.get("capa_status"),
                "isAdHoc": r.isAdHoc,
            })
        if not _is_terminal(r):
            open_iters.append({
                "checkpointCode": r.checkpointCode, "discipline": r.categoryName,
                "workflowState": r.workflowState, "round": r.currentRound, "ownerId": owner,
                "unassigned": not owner,
            })
            if r.criticality == "critical" and val == "fail":
                crit_open += 1
        if report_type == "FINAL":
            register.append({
                "checkpointCode": r.checkpointCode, "discipline": r.categoryName, "question": r.checkpointQuestion,
                "severity": r.criticality, "assessmentStatus": r.assessmentStatus, "workflowState": r.workflowState,
                "standard": r.standard, "requirementReference": r.requirementReference,
                "observation": r.observation, "isAdHoc": r.isAdHoc, "ownerId": owner,
                "capaNumber": capa.get("capa_number"),
                "auditorEvidenceIds": r.auditorEvidenceIds or [], "auditeeEvidenceIds": r.auditeeEvidenceIds or [],
                "interactions": [_interaction_to_dict(i) for i in sorted(r.interactions, key=lambda x: (x.timestamp, x.round))],
            })

    # Zero-assessable (e.g. all-NA / nothing assessed) audit: a 0% next to
    # "Conforming" is contradictory, so report a neutral NOT_ASSESSED result +
    # null score. NA counts as "answered" but not "assessable".
    assessable = score["passed"] + score["partially_passed"] + score["failed"]
    overall_pct = None if assessable == 0 else score["overall_score_pct"]
    overall_result = "NOT_ASSESSED" if assessable == 0 else _result_label(score)

    snapshot: dict[str, Any] = {
        "reportType": report_type,
        "auditCode": audit.auditNumber, "title": audit.title, "siteId": audit.plantId,
        "industryCode": audit.industryCode, "auditType": audit.auditType,
        "leadAuditorId": audit.leadAuditorUserId, "plantManagerId": audit.plantManagerUserId,
        "templateId": audit.templateId, "scopePresetUsed": audit.scopePresetUsed,
        "disciplinesInScope": audit.selectedDisciplineIds or [],
        "plannedDate": _iso(audit.scheduledDate), "submittedAt": _iso(audit.submittedAt), "closedAt": _iso(audit.closedAt),
        "overallScorePct": overall_pct, "overallResult": overall_result,
        "auditPassed": score["audit_passed"],
        "checkpointsTotal": total, "checkpointsAssessed": assessed,
        "passCount": score["passed"], "failCount": score["failed"],
        "partialCount": score["partially_passed"], "naCount": score["not_applicable"],
        "categoryScores": score["category_scores"],
        "criticalFailures": score["critical_failures"], "majorFailures": score["major_failures"],
        "minorFailures": score["minor_failures"],
        "openIterationsCount": len(open_iters), "criticalOpenCount": crit_open,
        "adHocCount": audit.adHocCount or 0,
        "capaSummary": {"total": capa_total, "open": capa_open, "overdue": capa_overdue},
        "findings": findings, "openIterations": open_iters,
    }
    if report_type == "FINAL":
        snapshot["checkpointRegister"] = register
        snapshot["standardsRollup"] = _standards_rollup(responses)
        snapshot["finalizability"] = _finalizability(audit)
    return snapshot


def _report_to_dict(rep: AuditReport) -> dict[str, Any]:
    return {
        "id": rep.id, "auditId": rep.auditId, "siteId": rep.siteId, "reportType": rep.reportType,
        "reportCode": rep.reportCode, "generatedById": rep.generatedById, "generatedAt": _iso(rep.generatedAt),
        "snapshot": rep.snapshot, "signOffs": rep.signOffs, "pdfAttachmentId": rep.pdfAttachmentId,
        "isSuperseded": rep.isSuperseded,
    }


async def generate_report(
    db: AsyncSession, *, user: User, audit_id: str, report_type: str, sign_offs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate an immutable Interim or Final report. Interim accumulates (the
    latest supersedes prior interims for display, all retained). Final requires
    a finalizable audit."""
    report_type = (report_type or "").upper()
    if report_type not in ("INTERIM", "FINAL"):
        raise ValueError("reportType must be INTERIM or FINAL")

    audit = await _load_audit(db, audit_id, with_responses=True, with_interactions=(report_type == "FINAL"))
    if audit is None:
        raise ValueError("Audit not found")

    if report_type == "INTERIM":
        if audit.status in ("scheduled", "cancelled"):
            raise ValueError("Nothing to report yet — start conducting the audit first")
    else:
        fin = _finalizability(audit)
        if not fin["finalizable"]:
            raise ValueError(
                f"{fin['blockerCount']} checkpoint(s) still in review — a final report needs every checkpoint terminal"
            )

    snapshot = _build_report_snapshot(audit, report_type)
    # Freeze friendly plant + actor names into the immutable snapshot so the
    # (external-facing) report shows names, not raw ids — and resolves
    # cross-plant actors the live /users picker can't.
    plant = await db.get(Plant, audit.plantId)
    snapshot["plantName"] = plant.name if plant else audit.plantId
    snapshot["plantCode"] = plant.code if plant else None
    uid_set: set[str] = {audit.leadAuditorUserId, audit.plantManagerUserId}
    for r in audit.responses:
        uid_set.update((r.assignedOwnerId, r.routedToUserId))
        if report_type == "FINAL":
            for i in r.interactions:
                uid_set.add(i.actorId)
    uid_set.update((so or {}).get("userId") for so in (sign_offs or []))
    uid_set = {u for u in uid_set if u}
    snapshot["userNames"] = {}
    if uid_set:
        rows = (await db.execute(select(User.id, User.name).where(User.id.in_(uid_set)))).all()
        snapshot["userNames"] = {uid: nm for uid, nm in rows}
    snapshot["generatedAt"] = _iso(_utcnow())
    snapshot["snapshotHash"] = _canonical_hash(snapshot)

    # Supersede prior reports of the same type for display (all retained).
    prior = (
        await db.execute(
            select(AuditReport).where(
                AuditReport.auditId == audit_id, AuditReport.reportType == report_type,
                AuditReport.isSuperseded.is_(False),
            )
        )
    ).scalars().all()
    for p in prior:
        p.isSuperseded = True

    base_n = (
        await db.execute(
            select(func.count(AuditReport.id)).where(
                AuditReport.auditId == audit_id, AuditReport.reportType == report_type
            )
        )
    ).scalar_one() or 0
    prefix = "I" if report_type == "INTERIM" else "F"

    # reportCode is derived from a count; under concurrent generation two
    # requests can pick the same number and collide on the unique constraint.
    # Insert inside a SAVEPOINT and retry with the next number on collision.
    for attempt in range(8):
        code = f"RPT-{audit.auditNumber}-{prefix}{base_n + 1 + attempt:02d}"
        rep = AuditReport(
            auditId=audit.id, siteId=audit.plantId, reportType=report_type, reportCode=code,
            generatedById=user.id, snapshot=snapshot, signOffs=sign_offs or None, isSuperseded=False,
        )
        try:
            async with db.begin_nested():
                db.add(rep)
                await db.flush()
            await db.refresh(rep)
            return _report_to_dict(rep)
        except IntegrityError:
            # The savepoint rollback already deassociated the failed INSERT from
            # the session — do NOT expunge it (that raises InvalidRequestError).
            continue
    raise ValueError("Could not allocate a unique report code — please retry")


async def list_reports(db: AsyncSession, audit_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditReport).where(AuditReport.auditId == audit_id).order_by(AuditReport.generatedAt.desc())
        )
    ).scalars().all()
    return [_report_to_dict(r) for r in rows]


async def get_report(db: AsyncSession, report_id: str) -> dict[str, Any] | None:
    rep = await db.get(AuditReport, report_id)
    return _report_to_dict(rep) if rep else None
