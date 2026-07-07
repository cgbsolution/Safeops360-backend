"""Daily Alert Brief API (build spec Part 2.3).

GET  /api/alerts                      — cursor-paginated feed (materialised cards only)
POST /api/alerts/{id}/ack             — acknowledge (audited; criticals allowed)
POST /api/alerts/{id}/mute            — mute 24h (non-critical only)
GET  /api/dashboard/daily-brief       — one aggregated payload for /dashboard/daily

The feed reads pre-computed Alert rows — impacts are NEVER resolved at read
time (the resolver job materialises them), which is what keeps this endpoint
inside the <500ms p95 budget.

NB: mounted UNGATED in dev like fire_safety/capture — the ALERTS licence code
exists in the registry but the signed dev licence predates it. Add
"alerts": "ALERTS" to ROUTER_MODULE once a licence including it is issued.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.alerts import Alert, DomainEvent
from app.models.capture import CaptureSubmission
from app.models.capa import Capa
from app.models.plant import Area, Plant
from app.models.rca import RootCauseAnalysis
from app.models.user import User
from app.services.access_scope import build_query_scope
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api", tags=["alerts"])

_READ = "ALERT.READ"
_ACK = "ALERT.ACK"
_MUTE = "ALERT.MUTE"

SEVERITY_ORDER = {"critical": 0, "attention": 1, "info": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


def _alert_out(a: Alert) -> dict[str, Any]:
    return {
        "id": a.id,
        "siteId": a.siteId,
        "severity": a.severity,
        "title": a.title,
        "bodyText": a.bodyText,
        "bodyTemplateKey": a.bodyTemplateKey,
        "bodyParams": a.bodyParams or {},
        "sourceEventType": a.sourceEventType,
        "impactedEntities": a.impactedEntities or [],
        "deepLink": a.deepLink,
        "dedupeKey": a.dedupeKey,
        "count": a.count,
        "status": a.status,
        "ackBy": a.ackBy,
        "ackAt": a.ackAt.isoformat() if a.ackAt else None,
        "mutedUntil": a.mutedUntil.isoformat() if a.mutedUntil else None,
        "createdAt": a.createdAt.isoformat() if a.createdAt else None,
        "updatedAt": a.updatedAt.isoformat() if a.updatedAt else None,
    }


def _feed_stmt(scope, site_id: str | None, window_hours: int, severity: str | None,
               status_filter: str | None, since: datetime | None):
    now = _now()
    stmt = (
        select(Alert)
        .where(Alert.isDeleted.is_(False))
        .where(Alert.createdAt >= now - timedelta(hours=window_hours))
    )
    stmt = scope.apply(stmt, Alert, plant_attr="siteId")
    if site_id:
        stmt = stmt.where(Alert.siteId == site_id)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if status_filter:
        stmt = stmt.where(Alert.status == status_filter)
    else:
        # default view: hide muted cards still inside their mute window
        stmt = stmt.where((Alert.mutedUntil.is_(None)) | (Alert.mutedUntil < now))
    if since is not None:
        stmt = stmt.where(Alert.updatedAt > since)
    return stmt


@router.get("/alerts")
async def list_alerts(
    since: datetime | None = Query(None),
    severity: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    site_id: str | None = Query(None, alias="siteId"),
    window: str = Query("24h"),
    limit: int = Query(100, ge=1, le=300),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    window_hours = 24 * 7 if window == "7d" else 24
    stmt = _feed_stmt(scope, site_id, window_hours, severity, status_filter, since)
    rows = (await db.execute(stmt.order_by(Alert.updatedAt.desc()).limit(limit))).scalars().all()
    items = sorted(
        (_alert_out(a) for a in rows),
        key=lambda x: (SEVERITY_ORDER.get(x["severity"], 3), x["updatedAt"] or ""),
    )
    cursor = max((r.updatedAt for r in rows), default=None)
    return {"items": items, "total": len(items), "cursor": cursor.isoformat() if cursor else None}


@router.post("/alerts/{alert_id}/ack")
async def acknowledge_alert(
    alert_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    alert = await db.get(Alert, alert_id)
    if alert is None or alert.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    await _require(db, user, _ACK, plant_id=alert.siteId)
    alert.status = "acknowledged"
    alert.ackBy = user.id
    alert.ackAt = _now()
    # explicit audit entry on top of the ORM capture (spec Part 3: every ack audited)
    from app.services.audit_log import record_event
    await record_event(
        db, entity_type="Alert", entity_id=alert.id, entity_code=alert.dedupeKey,
        plant_id=alert.siteId, action="SIGN_OFF",
        after={"status": "acknowledged", "ackBy": user.id}, reason="Alert acknowledged",
    )
    await db.commit()
    await db.refresh(alert)
    return _alert_out(alert)


@router.post("/alerts/{alert_id}/mute")
async def mute_alert(
    alert_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    alert = await db.get(Alert, alert_id)
    if alert is None or alert.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    await _require(db, user, _MUTE, plant_id=alert.siteId)
    if alert.severity == "critical":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Critical alerts cannot be muted — acknowledge them.")
    alert.status = "muted"
    alert.mutedUntil = _now() + timedelta(hours=24)
    from app.services.audit_log import record_event
    await record_event(
        db, entity_type="Alert", entity_id=alert.id, entity_code=alert.dedupeKey,
        plant_id=alert.siteId, action="STATE_TRANSITION",
        after={"status": "muted", "mutedUntil": alert.mutedUntil.isoformat()}, reason="Alert muted 24h",
    )
    await db.commit()
    await db.refresh(alert)
    return _alert_out(alert)


# ── Daily brief aggregate (one call renders the whole page) ──────────────────
OPEN_CAPA_STATES = (
    "DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION",
)


async def _count(db: AsyncSession, stmt) -> int:
    return (await db.execute(stmt)).scalar_one()


@router.get("/dashboard/daily-brief")
async def daily_brief(
    site_id: str | None = Query(None, alias="siteId"),
    window: str = Query("24h"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    scope = await build_query_scope(db, user.id, _READ)
    now = _now()
    window_hours = 24 * 7 if window == "7d" else 24
    window_start = now - timedelta(hours=window_hours)
    prev_start = window_start - timedelta(hours=window_hours)

    # sites the caller may switch between (leader rollup)
    if scope.all_plants:
        plants = (await db.execute(select(Plant).order_by(Plant.code))).scalars().all()
    else:
        plants = (
            await db.execute(select(Plant).where(Plant.id.in_(scope.plant_ids or [])).order_by(Plant.code))
        ).scalars().all()
    sites = [{"id": p.id, "code": p.code, "name": p.name} for p in plants]
    effective_site = site_id or (sites[0]["id"] if len(sites) == 1 else None)

    # ── the feed ──
    feed_stmt = _feed_stmt(scope, effective_site, window_hours, None, None, None)
    alerts = (await db.execute(feed_stmt.order_by(Alert.updatedAt.desc()).limit(200))).scalars().all()
    feed = sorted(
        (_alert_out(a) for a in alerts),
        key=lambda x: (SEVERITY_ORDER.get(x["severity"], 3), (x["updatedAt"] or "")),
    )
    ack_week = await _count(
        db,
        scope.apply(
            select(func.count()).select_from(Alert)
            .where(Alert.isDeleted.is_(False))
            .where(Alert.status == "acknowledged")
            .where(Alert.updatedAt >= now - timedelta(days=7)),
            Alert, plant_attr="siteId",
        ),
    )

    # ── today's numbers + deltas (windowed event/record counts) ──
    def _sited(stmt, model, attr="plantId"):
        stmt = scope.apply(stmt, model, plant_attr=attr)
        if effective_site:
            stmt = stmt.where(getattr(model, attr) == effective_site)
        return stmt

    async def _windowed(model, attr, time_col, extra=None) -> tuple[int, int]:
        base = select(func.count()).select_from(model)
        if extra is not None:
            base = extra(base)
        cur = await _count(db, _sited(base.where(time_col >= window_start), model, attr))
        prev = await _count(
            db, _sited(base.where(time_col >= prev_start).where(time_col < window_start), model, attr)
        )
        return cur, prev

    new_obs, prev_obs = await _windowed(
        CaptureSubmission, "plantId", CaptureSubmission.createdAt,
        lambda s: s.where(CaptureSubmission.isDeleted.is_(False)),
    )

    from app.models.near_miss import NearMiss
    open_nm = await _count(db, _sited(
        select(func.count()).select_from(NearMiss).where(NearMiss.status != "CLOSED"), NearMiss))

    rcas_in_progress = await _count(db, _sited(
        select(func.count()).select_from(RootCauseAnalysis)
        .where(RootCauseAnalysis.isDeleted.is_(False))
        .where(RootCauseAnalysis.status.in_(("DRAFT", "IN_ANALYSIS", "PEER_REVIEW"))),
        RootCauseAnalysis))

    from app.models.permit import Permit, PermitStatus
    active_ptw = await _count(db, _sited(
        select(func.count()).select_from(Permit).where(Permit.status == PermitStatus.ACTIVE), Permit))

    capas_due_7d = await _count(db, _sited(
        select(func.count()).select_from(Capa)
        .where(Capa.state.in_(OPEN_CAPA_STATES))
        .where(Capa.closureTargetDate.is_not(None))
        .where(Capa.closureTargetDate <= now + timedelta(days=7)),
        Capa))

    # deltas that read the outbox (cheap indexed counts on DomainEvent)
    async def _event_delta(event_type: str) -> tuple[int, int]:
        base = select(func.count()).select_from(DomainEvent).where(DomainEvent.eventType == event_type)
        if effective_site:
            base = base.where(DomainEvent.siteId == effective_site)
        cur = await _count(db, base.where(DomainEvent.occurredAt >= window_start))
        prev = await _count(
            db, base.where(DomainEvent.occurredAt >= prev_start).where(DomainEvent.occurredAt < window_start)
        )
        return cur, prev

    high_triage_cur, high_triage_prev = await _event_delta("observation.triaged_high")

    numbers = [
        {"key": "newFieldReports", "label": "New field reports", "value": new_obs, "delta": new_obs - prev_obs},
        {"key": "highTriaged", "label": "Triaged HIGH+", "value": high_triage_cur, "delta": high_triage_cur - high_triage_prev},
        {"key": "openNearMisses", "label": "Open near-misses", "value": open_nm, "delta": None},
        {"key": "rcasInProgress", "label": "RCAs in progress", "value": rcas_in_progress, "delta": None},
        {"key": "activePermits", "label": "Active permits", "value": active_ptw, "delta": None},
        {"key": "capasDue7d", "label": "CAPAs due ≤7d", "value": capas_due_7d, "delta": None},
    ]

    # ── field pulse (the Part-1 adoption story) ──
    pulse_rows = (
        await db.execute(
            _sited(
                select(CaptureSubmission.areaId, func.count())
                .where(CaptureSubmission.isDeleted.is_(False))
                .where(CaptureSubmission.createdAt >= now - timedelta(hours=24))
                .group_by(CaptureSubmission.areaId),
                CaptureSubmission,
            )
        )
    ).all()
    area_ids = [r[0] for r in pulse_rows if r[0]]
    area_names: dict[str, str] = {}
    if area_ids:
        for area in (await db.execute(select(Area).where(Area.id.in_(area_ids)))).scalars().all():
            area_names[area.id] = area.name
    pulse_by_area = sorted(
        ({"area": area_names.get(r[0], "Unassigned") if r[0] else "Unassigned", "count": r[1]} for r in pulse_rows),
        key=lambda x: -x["count"],
    )[:8]

    pulse_base = (
        select(func.count()).select_from(CaptureSubmission)
        .where(CaptureSubmission.isDeleted.is_(False))
        .where(CaptureSubmission.createdAt >= now - timedelta(hours=24))
    )
    pulse_total = await _count(db, _sited(pulse_base, CaptureSubmission))
    pulse_voice = await _count(db, _sited(pulse_base.where(CaptureSubmission.voiceLangCode.is_not(None)), CaptureSubmission))
    pulse_offline = await _count(db, _sited(pulse_base.where(CaptureSubmission.wasOffline.is_(True)), CaptureSubmission))

    # ── aging watch: 5 oldest open RCA/CAPA ──
    old_rcas = (
        await db.execute(
            _sited(
                select(RootCauseAnalysis)
                .where(RootCauseAnalysis.isDeleted.is_(False))
                .where(RootCauseAnalysis.status.in_(("DRAFT", "IN_ANALYSIS", "PEER_REVIEW")))
                .order_by(RootCauseAnalysis.createdAt.asc()).limit(5),
                RootCauseAnalysis,
            )
        )
    ).scalars().all()
    old_capas = (
        await db.execute(
            _sited(
                select(Capa).where(Capa.state.in_(OPEN_CAPA_STATES))
                .order_by(Capa.createdAt.asc()).limit(5),
                Capa,
            )
        )
    ).scalars().all()

    def _age_days(dt: datetime | None) -> int:
        if dt is None:
            return 0
        aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return max(0, (now - aware).days)

    aging = sorted(
        [
            *(
                {"type": "RCA", "ref": r.rcaCode, "label": r.title, "ageDays": _age_days(r.createdAt), "href": f"/erm/rca/{r.id}"}
                for r in old_rcas
            ),
            *(
                {"type": "CAPA", "ref": c.capaNumber, "label": c.title, "ageDays": _age_days(c.createdAt), "href": f"/capa/{c.id}"}
                for c in old_capas
            ),
        ],
        key=lambda x: -x["ageDays"],
    )[:5]

    return {
        "generatedAt": now.isoformat(),
        "window": window,
        "sites": sites,
        "siteId": effective_site,
        "feed": feed,
        "acknowledgedThisWeek": ack_week,
        "numbers": numbers,
        "fieldPulse": {
            "total": pulse_total,
            "voicePct": round(100 * pulse_voice / pulse_total) if pulse_total else 0,
            "offlinePct": round(100 * pulse_offline / pulse_total) if pulse_total else 0,
            "byArea": pulse_by_area,
        },
        "agingWatch": aging,
    }
