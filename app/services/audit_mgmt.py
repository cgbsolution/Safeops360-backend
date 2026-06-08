"""Audit Management service (Pharma IMS Module 4).

Audit lifecycle: plan → execute (capture findings) → issue report (e-signed) →
auditee response → findings spawn CAPA (via the existing AUDIT_* source types)
→ CAPA monitoring → closure (e-signed). Service functions flush; router commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_mgmt import Audit, AuditFinding
from app.models.plant import Plant
from app.models.user import User
from app.services import part11

_TYPE_ABBR = {
    "internal_gmp": "INT", "internal_hse": "INT", "internal_integrated": "INT",
    "supplier_audit": "SUP", "regulatory_inspection": "REG", "customer_audit": "CUS",
    "certification_audit": "CER", "mock_inspection": "MOCK",
}
_CAPA_SOURCE = {
    "internal_gmp": "AUDIT_INTERNAL", "internal_hse": "AUDIT_INTERNAL", "internal_integrated": "AUDIT_INTERNAL",
    "regulatory_inspection": "AUDIT_REGULATORY", "supplier_audit": "AUDIT_EXTERNAL",
    "customer_audit": "AUDIT_EXTERNAL", "certification_audit": "AUDIT_EXTERNAL", "mock_inspection": "AUDIT_INTERNAL",
}
_FINDING_SEVERITY = {"critical": "CRITICAL", "major": "HIGH", "minor": "MODERATE", "observation": "LOW", "opportunity_for_improvement": "LOW"}
_RESPONSE_DAYS = {"critical": 5, "major": 15, "minor": 30, "observation": 30, "opportunity_for_improvement": 45}
OPEN_STATUSES = ("planned", "in_progress", "complete_pending_report", "report_issued", "response_pending", "capa_monitoring")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def audit_snapshot(a: Audit) -> dict[str, Any]:
    return {"number": a.number, "title": a.title, "auditType": a.auditType, "plannedStart": _iso(a.plannedStart)}


async def _plant_code(db: AsyncSession, plant_id: str) -> str:
    p = await db.get(Plant, plant_id)
    return (p.code if p and p.code else plant_id[:4]).upper()


async def _next_number(db: AsyncSession, audit_type: str) -> str:
    year = _utcnow().year
    abbr = _TYPE_ABBR.get(audit_type, "AUD")
    n = (await db.execute(select(func.count(Audit.id)).where(Audit.number.like(f"AUD-{year}-{abbr}-%")))).scalar_one()
    return f"AUD-{year}-{abbr}-{(n + 1):04d}"


async def create_audit(db: AsyncSession, *, user: User, data: dict[str, Any]) -> Audit:
    now = _utcnow()
    a = Audit(
        tenantId=None, number=await _next_number(db, data["auditType"]), title=data["title"],
        description=data.get("description", ""), auditType=data["auditType"], plantId=data["plantId"],
        scope=data.get("scope", []), applicableStandards=data.get("applicableStandards", []),
        regulatoryAuthority=data.get("regulatoryAuthority"), supplierName=data.get("supplierName"),
        plannedStart=data.get("plannedStart") or now, plannedEnd=data.get("plannedEnd") or (now + timedelta(days=2)),
        leadAuditorUserId=data.get("leadAuditorUserId") or user.id, auditTeam=data.get("auditTeam", []),
        auditeeDepartmentHeadUserId=data.get("auditeeDepartmentHeadUserId"),
        regulatoryCommitments=[], status="planned", createdByUserId=user.id,
    )
    db.add(a)
    await db.flush()
    await part11.write_audit(db, record_type="audit", record_id=a.id, record_number=a.number,
                             event_type="created", user=user, new_value=a.title, reason="Audit planned")
    return a


async def start_audit(db: AsyncSession, *, a: Audit, user: User) -> Audit:
    a.actualStart = _utcnow()
    old = a.status
    a.status = "in_progress"
    await part11.write_audit(db, record_type="audit", record_id=a.id, record_number=a.number,
                             event_type="status_changed", user=user, field_name="status", old_value=old, new_value="in_progress", reason="Audit started")
    return a


async def add_finding(db: AsyncSession, *, a: Audit, user: User, type_: str, description: str,
                      area: str = "", reference_requirement: str = "", evidence: str = "") -> AuditFinding:
    n = (await db.execute(select(func.count(AuditFinding.id)).where(AuditFinding.auditId == a.id))).scalar_one()
    f = AuditFinding(
        auditId=a.id, findingNumber=f"{a.number}-F{(n + 1):02d}", type=type_, area=area, description=description,
        referenceRequirement=reference_requirement, evidence=evidence,
        responseDueDate=_utcnow() + timedelta(days=_RESPONSE_DAYS.get(type_, 30)), findingStatus="open",
    )
    db.add(f)
    await db.flush()
    await part11.write_audit(db, record_type="audit", record_id=a.id, record_number=a.number,
                             event_type="modified", user=user, field_name="finding", new_value=f"{f.findingNumber} [{type_}]", reason="Finding raised")
    return f


async def issue_report(db: AsyncSession, *, a: Audit, user: User, password: str, ip: str | None = None) -> Audit:
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — report not issued.")
    now = _utcnow()
    a.reportIssuedAt = now
    a.actualEnd = a.actualEnd or now
    old = a.status
    a.status = "response_pending"
    await part11.sign(db, user=user, record_type="audit", record_id=a.id, record_number=a.number,
                      meaning="Audit Report Issued", record_snapshot=audit_snapshot(a), ip=ip)
    await part11.write_audit(db, record_type="audit", record_id=a.id, record_number=a.number,
                             event_type="status_changed", user=user, field_name="status", old_value=old, new_value="response_pending", reason="Audit report issued")
    return a


async def respond_finding(db: AsyncSession, *, finding: AuditFinding, audit: Audit, user: User, response: str) -> AuditFinding:
    finding.auditeeResponse = response
    if finding.findingStatus == "open":
        finding.findingStatus = "response_pending"
    await part11.write_audit(db, record_type="audit", record_id=audit.id, record_number=audit.number,
                             event_type="modified", user=user, field_name=f"finding:{finding.findingNumber}", new_value="response recorded", reason="Auditee response")
    return finding


async def raise_capa_for_finding(db: AsyncSession, *, audit: Audit, finding: AuditFinding, user: User,
                                 owner_user_id: str | None = None) -> dict[str, Any]:
    from app.routers.capa import create_capa
    from app.schemas.capa import CapaCreate

    problem = (f"Audit {audit.number} ({audit.auditType}) finding {finding.findingNumber} [{finding.type}] "
               f"in area '{finding.area}': {finding.description}. Reference: {finding.referenceRequirement or 'n/a'}. "
               f"Evidence: {finding.evidence or 'see report'}. CAPA to correct and prevent recurrence.")
    if len(problem) < 50:
        problem += " GMP corrective and preventive action required per ICH Q10."
    title = (finding.description or f"Audit finding {finding.findingNumber}")[:120]
    if len(title) < 4:
        title = f"Audit finding {finding.findingNumber}"

    payload = CapaCreate(
        plantId=audit.plantId, sourceTypeCode=_CAPA_SOURCE.get(audit.auditType, "AUDIT_INTERNAL"),
        sourceReferenceId=finding.id, sourceReferenceUrl=f"/audits/{audit.id}",
        sourceReferenceSummary=f"Audit {audit.number} finding {finding.findingNumber}",
        sourceMetadata={"auditNumber": audit.number, "findingNumber": finding.findingNumber, "findingType": finding.type},
        title=title, problemDescription=problem, detectedAt=audit.actualStart or _utcnow(),
        primaryCategory="Quality / Audit Finding", severity=_FINDING_SEVERITY.get(finding.type, "MODERATE"),
        priority=_FINDING_SEVERITY.get(finding.type, "MODERATE"),
        primaryOwnerUserId=owner_user_id or audit.auditeeDepartmentHeadUserId or user.id,
    )
    capa = await create_capa(payload, user=user, db=db)
    finding.capaId = capa.id
    finding.capaNumber = capa.capaNumber
    finding.capaStatus = capa.state
    finding.findingStatus = "capa_in_progress"
    if audit.status == "response_pending":
        old = audit.status
        audit.status = "capa_monitoring"
        await part11.write_audit(db, record_type="audit", record_id=audit.id, record_number=audit.number,
                                 event_type="status_changed", user=user, field_name="status", old_value=old, new_value="capa_monitoring", reason="CAPA raised for finding")
    await part11.write_audit(db, record_type="audit", record_id=audit.id, record_number=audit.number,
                             event_type="modified", user=user, field_name=f"finding:{finding.findingNumber}", new_value=capa.capaNumber, reason="CAPA raised from finding")
    return {"capaId": capa.id, "capaNumber": capa.capaNumber}


async def close_finding(db: AsyncSession, *, finding: AuditFinding, audit: Audit, user: User) -> AuditFinding:
    finding.findingStatus = "closed"
    finding.closedAt = _utcnow()
    await part11.write_audit(db, record_type="audit", record_id=audit.id, record_number=audit.number,
                             event_type="modified", user=user, field_name=f"finding:{finding.findingNumber}", new_value="closed", reason="Finding closed")
    return finding


async def close_audit(db: AsyncSession, *, a: Audit, user: User, password: str, ip: str | None = None) -> Audit:
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — audit not closed.")
    a.closedAt = _utcnow()
    old = a.status
    a.status = "closed"
    await part11.sign(db, user=user, record_type="audit", record_id=a.id, record_number=a.number,
                      meaning="Reviewed and Approved — Audit Closure", record_snapshot=audit_snapshot(a), ip=ip)
    await part11.write_audit(db, record_type="audit", record_id=a.id, record_number=a.number,
                             event_type="closed", user=user, field_name="status", old_value=old, new_value="closed", reason="Audit closed")
    return a


def finding_dict(f: AuditFinding) -> dict[str, Any]:
    now = _utcnow()
    due = f.responseDueDate if (f.responseDueDate is None or f.responseDueDate.tzinfo) else f.responseDueDate.replace(tzinfo=timezone.utc)
    overdue = bool(due and f.findingStatus not in ("closed",) and now > due)
    return {
        "id": f.id, "findingNumber": f.findingNumber, "type": f.type, "area": f.area, "description": f.description,
        "referenceRequirement": f.referenceRequirement, "evidence": f.evidence, "auditeeResponse": f.auditeeResponse,
        "responseDueDate": _iso(f.responseDueDate), "overdue": overdue,
        "capaId": f.capaId, "capaNumber": f.capaNumber, "findingStatus": f.findingStatus, "closedAt": _iso(f.closedAt),
    }


def to_dict(a: Audit, findings: list[AuditFinding] | None = None) -> dict[str, Any]:
    fs = findings if findings is not None else (a.findings if "findings" in a.__dict__ else [])
    counts: dict[str, int] = {}
    for f in fs:
        counts[f.type] = counts.get(f.type, 0) + 1
    return {
        "id": a.id, "number": a.number, "title": a.title, "description": a.description, "auditType": a.auditType,
        "plantId": a.plantId, "status": a.status, "scope": a.scope or [], "applicableStandards": a.applicableStandards or [],
        "regulatoryAuthority": a.regulatoryAuthority, "supplierName": a.supplierName,
        "plannedStart": _iso(a.plannedStart), "plannedEnd": _iso(a.plannedEnd),
        "actualStart": _iso(a.actualStart), "actualEnd": _iso(a.actualEnd),
        "leadAuditorUserId": a.leadAuditorUserId, "reportIssuedAt": _iso(a.reportIssuedAt),
        "findingCount": len(fs), "findingsByType": counts,
        "openFindings": sum(1 for f in fs if f.findingStatus != "closed"),
        "createdAt": _iso(a.createdAt), "closedAt": _iso(a.closedAt),
    }


async def dashboard(db: AsyncSession, *, plant_id: str) -> dict[str, Any]:
    audits = (await db.execute(select(Audit).where(Audit.plantId == plant_id))).scalars().all()
    findings = (await db.execute(
        select(AuditFinding).join(Audit, Audit.id == AuditFinding.auditId).where(Audit.plantId == plant_id)
    )).scalars().all()
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for a in audits:
        by_status[a.status] = by_status.get(a.status, 0) + 1
        by_type[a.auditType] = by_type.get(a.auditType, 0) + 1
    open_findings = sum(1 for f in findings if f.findingStatus != "closed")
    critical = sum(1 for f in findings if f.type == "critical" and f.findingStatus != "closed")
    capa_linked = sum(1 for f in findings if f.capaId)
    return {
        "plantId": plant_id, "total": len(audits), "open": sum(1 for a in audits if a.status in OPEN_STATUSES),
        "totalFindings": len(findings), "openFindings": open_findings, "criticalOpen": critical, "findingsWithCapa": capa_linked,
        "byStatus": by_status, "byType": by_type,
    }
