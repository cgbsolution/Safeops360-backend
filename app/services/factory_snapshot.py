"""Facilities — live compliance provider + snapshot precompute (Phase D).

Reads operational metrics per site (= Plant.id) from the existing engines —
CAMS, CAPA, Audit & Compliance, Incidents, ERM Obligations — and never keeps a
duplicate store: `FactoryComplianceSnapshot` is only a cache of these live reads
so the consolidated dashboard renders fast. Every engine is imported and queried
defensively, so a deployment missing one engine simply drops its metric
(graceful degradation, hard constraint §4).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import FactoryCertification, FactoryComplianceSnapshot
from app.services import factory as fsvc

# ── engine models (optional — degrade gracefully if a module is absent) ──────
try:
    from app.models.cams import CamsEngagement, CamsFinding
except Exception:  # pragma: no cover
    CamsEngagement = CamsFinding = None  # type: ignore
try:
    from app.models.capa import Capa
except Exception:  # pragma: no cover
    Capa = None  # type: ignore
try:
    from app.models.audit_compliance import ComplianceAudit
except Exception:  # pragma: no cover
    ComplianceAudit = None  # type: ignore
try:
    from app.models.incident import Incident
except Exception:  # pragma: no cover
    Incident = None  # type: ignore
try:
    from app.models.erm_p2 import LegalObligation
except Exception:  # pragma: no cover
    LegalObligation = None  # type: ignore

CAMS_FINDING_CLOSED = ("CLOSED", "ACCEPTED_RISK")
CAMS_CRIT = ("MAJOR_NC", "CRITICAL_NC")
CAPA_CLOSED = ("CLOSED", "CLOSED_RECURRED", "CANCELLED")
# LegalObligation.status: COMPLIANT | DUE_SOON | OVERDUE | UNDER_RENEWAL | NOT_APPLICABLE
OBLIGATION_OK = ("COMPLIANT", "NOT_APPLICABLE")
OBLIGATION_OVERDUE = ("OVERDUE",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# Live metric computation (read-only across the engines)
# ════════════════════════════════════════════════════════════════════════════
async def compute_site_metrics(db: AsyncSession, site_id: str) -> dict[str, Any]:
    m: dict[str, Any] = {
        "auditComplianceScorePct": None,
        "openFindings": 0,
        "criticalFindings": 0,
        "openCapas": 0,
        "overdueCapas": 0,
        "openObligations": 0,
        "overdueObligations": 0,
        "incidentCount12m": 0,
        "lastAuditDate": None,
    }

    # ── CAMS engagements → compliance score + last audit ──
    if CamsEngagement is not None:
        try:
            engs = (
                await db.execute(
                    select(CamsEngagement).where(CamsEngagement.siteId == site_id).where(CamsEngagement.isDeleted.is_(False))
                )
            ).scalars().all()
            scored = [e.scorePercent for e in engs if e.scorePercent is not None and e.conductedDate is not None]
            if scored:
                m["auditComplianceScorePct"] = round(sum(scored) / len(scored), 1)
            conducted = [e.conductedDate for e in engs if e.conductedDate]
            if conducted:
                m["lastAuditDate"] = max(conducted)
        except Exception:
            pass

    # ── CAMS findings → open / critical ──
    if CamsFinding is not None:
        try:
            finds = (
                await db.execute(
                    select(CamsFinding).where(CamsFinding.siteId == site_id).where(CamsFinding.isDeleted.is_(False))
                )
            ).scalars().all()
            open_f = [f for f in finds if f.status not in CAMS_FINDING_CLOSED]
            m["openFindings"] = len(open_f)
            m["criticalFindings"] = sum(1 for f in open_f if f.severity in CAMS_CRIT)
        except Exception:
            pass

    # ── Audit & Compliance fallback for score / last audit ──
    if m["auditComplianceScorePct"] is None and ComplianceAudit is not None:
        try:
            audits = (await db.execute(select(ComplianceAudit).where(ComplianceAudit.plantId == site_id))).scalars().all()
            scored = [a.overallCompliancePct for a in audits if a.overallCompliancePct is not None]
            if scored:
                m["auditComplianceScorePct"] = round(sum(scored) / len(scored), 1)
            dates = [(a.submittedAt or a.closedAt) for a in audits if (a.submittedAt or a.closedAt)]
            if dates and not m["lastAuditDate"]:
                m["lastAuditDate"] = max(dates)
        except Exception:
            pass

    # ── CAPA → open / overdue ──
    if Capa is not None:
        try:
            capas = (await db.execute(select(Capa).where(Capa.plantId == site_id))).scalars().all()
            open_capas = [c for c in capas if c.state not in CAPA_CLOSED]
            m["openCapas"] = len(open_capas)
            now = _now()
            m["overdueCapas"] = sum(
                1 for c in open_capas if getattr(c, "closureTargetDate", None) and _aware(c.closureTargetDate) < now
            )
        except Exception:
            pass

    # ── Incidents → 12-month count ──
    if Incident is not None:
        try:
            cutoff = _now().replace(tzinfo=None) - timedelta(days=365)
            n = (
                await db.execute(
                    select(func.count()).select_from(Incident).where(Incident.plantId == site_id).where(Incident.date >= cutoff)
                )
            ).scalar() or 0
            m["incidentCount12m"] = int(n)
        except Exception:
            pass

    # ── ERM Obligations → open / overdue ──
    if LegalObligation is not None:
        try:
            obs = (
                await db.execute(
                    select(LegalObligation).where(LegalObligation.siteId == site_id).where(LegalObligation.isDeleted.is_(False))
                )
            ).scalars().all()
            m["openObligations"] = sum(1 for o in obs if o.status not in OBLIGATION_OK)
            m["overdueObligations"] = sum(1 for o in obs if o.status in OBLIGATION_OVERDUE)
        except Exception:
            pass

    return m


async def _certs_expiring(db: AsyncSession, profile_id: str) -> int:
    certs = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == profile_id)
            .where(FactoryCertification.isDeleted.is_(False))
        )
    ).scalars().all()
    return sum(
        1 for c in certs if fsvc.cert_is_expiring(fsvc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status))
    )


# ════════════════════════════════════════════════════════════════════════════
# Snapshot precompute (upsert the LIVE row per factory)
# ════════════════════════════════════════════════════════════════════════════
async def recompute_snapshot(db: AsyncSession, profile, user_id: str | None = None) -> FactoryComplianceSnapshot:
    m = await compute_site_metrics(db, profile.siteId)
    certs_expiring = await _certs_expiring(db, profile.id)
    snap = (
        await db.execute(
            select(FactoryComplianceSnapshot)
            .where(FactoryComplianceSnapshot.factoryProfileId == profile.id)
            .where(FactoryComplianceSnapshot.periodLabel == "LIVE")
        )
    ).scalar_one_or_none()
    if snap is None:
        snap = FactoryComplianceSnapshot(factoryProfileId=profile.id, siteId=profile.siteId, periodLabel="LIVE", createdBy=user_id)
        db.add(snap)
    snap.auditComplianceScorePct = m["auditComplianceScorePct"]
    snap.openFindings = m["openFindings"]
    snap.criticalFindings = m["criticalFindings"]
    snap.openCapas = m["openCapas"]
    snap.overdueCapas = m["overdueCapas"]
    snap.openObligations = m["openObligations"]
    snap.overdueObligations = m["overdueObligations"]
    snap.certsExpiringCount = certs_expiring
    snap.lastAuditDate = m["lastAuditDate"]
    snap.incidentCount12m = m["incidentCount12m"]
    snap.computedAt = _now()
    snap.updatedBy = user_id
    return snap


# ════════════════════════════════════════════════════════════════════════════
# F-02 Compliance & Audit tab — live, drillable lists per engine
# ════════════════════════════════════════════════════════════════════════════
async def compliance_detail(db: AsyncSession, site_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"audits": [], "findings": [], "capas": [], "obligations": [], "incidents": []}

    if CamsEngagement is not None:
        try:
            engs = (
                await db.execute(
                    select(CamsEngagement)
                    .where(CamsEngagement.siteId == site_id)
                    .where(CamsEngagement.isDeleted.is_(False))
                    .order_by(CamsEngagement.plannedDate.desc())
                    .limit(10)
                )
            ).scalars().all()
            out["audits"] = [
                {
                    "id": e.id, "code": e.engagementCode, "title": e.title, "type": e.engagementType,
                    "status": e.status, "score": e.scorePercent,
                    "conductedDate": e.conductedDate.isoformat() if e.conductedDate else None,
                }
                for e in engs
            ]
        except Exception:
            pass
    if CamsFinding is not None:
        try:
            finds = (
                await db.execute(
                    select(CamsFinding)
                    .where(CamsFinding.siteId == site_id)
                    .where(CamsFinding.isDeleted.is_(False))
                    .order_by(CamsFinding.createdAt.desc())
                    .limit(15)
                )
            ).scalars().all()
            out["findings"] = [
                {"id": f.id, "code": f.findingCode, "title": f.title, "severity": f.severity, "status": f.status}
                for f in finds
            ]
        except Exception:
            pass
    if Capa is not None:
        try:
            capas = (
                await db.execute(select(Capa).where(Capa.plantId == site_id).order_by(Capa.detectedAt.desc()).limit(15))
            ).scalars().all()
            now = _now()
            out["capas"] = [
                {
                    "id": c.id, "number": getattr(c, "capaNumber", None) or getattr(c, "number", None),
                    "title": getattr(c, "title", None), "state": c.state, "severity": getattr(c, "severity", None),
                    "overdue": bool(getattr(c, "closureTargetDate", None) and c.state not in CAPA_CLOSED and _aware(c.closureTargetDate) < now),
                }
                for c in capas
            ]
        except Exception:
            pass
    if LegalObligation is not None:
        try:
            obs = (
                await db.execute(
                    select(LegalObligation)
                    .where(LegalObligation.siteId == site_id)
                    .where(LegalObligation.isDeleted.is_(False))
                    .order_by(LegalObligation.validUntil.asc().nulls_last())
                    .limit(15)
                )
            ).scalars().all()
            out["obligations"] = [
                {
                    "id": o.id, "code": getattr(o, "obligationCode", None), "title": getattr(o, "title", None),
                    "status": o.status, "validUntil": o.validUntil.isoformat() if getattr(o, "validUntil", None) else None,
                }
                for o in obs
            ]
        except Exception:
            pass
    if Incident is not None:
        try:
            cutoff = _now().replace(tzinfo=None) - timedelta(days=365)
            incs = (
                await db.execute(
                    select(Incident).where(Incident.plantId == site_id).where(Incident.date >= cutoff).order_by(Incident.date.desc()).limit(15)
                )
            ).scalars().all()
            out["incidents"] = [
                {
                    "id": i.id, "number": getattr(i, "number", None),
                    "type": getattr(i, "type", None).value if getattr(getattr(i, "type", None), "value", None) else str(getattr(i, "type", "")),
                    "status": getattr(i, "status", None).value if getattr(getattr(i, "status", None), "value", None) else str(getattr(i, "status", "")),
                    "date": i.date.isoformat() if getattr(i, "date", None) else None,
                }
                for i in incs
            ]
        except Exception:
            pass

    return out
