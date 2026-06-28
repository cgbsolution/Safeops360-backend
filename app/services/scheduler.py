"""Background job scheduler (P2-1) — dependency-free asyncio supervisor (no
APScheduler/Celery needed for the single-process on-prem deployment; mirrors the
existing _licence_recheck_loop pattern).

Every registered job:
  • wraps an idempotent service function
  • runs in its own DB session with a SystemActor (audit attribution + scoped jobs)
  • records a JobRun (RUNNING → SUCCESS/FAILED + summary + recordsAffected)
  • is callable on-demand (run-now) AND on its interval
Misfire recovery: on startup the supervisor reads the last successful JobRun per
job; an overdue job runs once (not N times).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.core.db import AsyncSessionLocal

log = logging.getLogger("safeops360.scheduler")

HOUR = 3600
DAY = 24 * HOUR


@dataclass
class Job:
    id: str
    label: str
    interval_seconds: int
    fn: Callable[[Any], Awaitable[dict]]  # async (db) -> summary dict


# ── Job implementations (each idempotent; opens nothing — gets the db) ───────
async def _job_kri_feeds(db) -> dict:
    from app.services.erm_p2 import run_module_fed
    return await run_module_fed(db)


async def _job_appetite_eval(db) -> dict:
    from app.services.erm_p2 import evaluate_appetite
    return await evaluate_appetite(db)


async def _job_loss_auto_feed(db) -> dict:
    from app.services.erm_p2 import auto_feed_incidents
    return await auto_feed_incidents(db, actor_id="SYSTEM:loss_auto_feed")


async def _job_rollup(db) -> dict:
    from app.models.erm import RollupRule
    from app.services.erm import run_rollup_rule
    rules = (await db.execute(select(RollupRule).where(RollupRule.isActive.is_(True)).where(RollupRule.isDeleted.is_(False)))).scalars().all()
    created = updated = 0
    for r in rules:
        res = await run_rollup_rule(db, r)
        created += res.get("created", 0)
        updated += res.get("updated", 0)
    return {"rules": len(rules), "created": created, "updated": updated}


async def _job_incident_alerts(db) -> dict:
    from app.services.erm import sync_incident_risk_alerts
    return await sync_incident_risk_alerts(db)


async def _job_cams_repeats(db) -> dict:
    from app.services.cams_analytics import detect_repeat_findings
    return await detect_repeat_findings(db)


async def _job_fire_status(db) -> dict:
    from app.services.fire_safety import recompute_all_statuses
    return await recompute_all_statuses(db)


async def _job_compliance(db) -> dict:
    from app.services.erm_p2 import generate_tasks, refresh_statuses
    a = await refresh_statuses(db)
    b = await generate_tasks(db)
    return {**a, **b}


async def _job_audit_integrity(db) -> dict:
    """Sample the audit chain: verify the most-recently-touched entities."""
    from app.models.audit_log import AuditLog
    from app.services.audit_log import verify_chain
    pairs = (
        await db.execute(select(AuditLog.entityType, AuditLog.entityId).distinct().limit(200))
    ).all()
    checked = broken = 0
    for et, eid in pairs:
        v = await verify_chain(db, et, eid)
        checked += 1
        if not v["intact"]:
            broken += 1
            log.warning("audit chain BROKEN for %s/%s at seq %s", et, eid, v["brokenAtSequence"])
    return {"chainsChecked": checked, "chainsBroken": broken}


JOBS: dict[str, Job] = {j.id: j for j in [
    Job("kri_module_feeds", "KRI module feeds", 1 * HOUR, _job_kri_feeds),
    Job("loss_auto_feed", "Incident → Loss Event auto-feed", 2 * HOUR, _job_loss_auto_feed),
    Job("erm_rollup", "HIRA/EAI → CRR → ERM rollup", 4 * HOUR, _job_rollup),
    Job("appetite_eval", "Risk appetite breach evaluation", 4 * HOUR, _job_appetite_eval),
    Job("incident_risk_alerts", "Incident → ERM risk review flag (I-04)", 6 * HOUR, _job_incident_alerts),
    Job("cams_repeat_findings", "CAMS repeat-finding detection", 12 * HOUR, _job_cams_repeats),
    Job("fire_equipment_status", "Fire equipment status recompute", 1 * DAY, _job_fire_status),
    Job("compliance_tasks", "Compliance status + task generation", 1 * DAY, _job_compliance),
    Job("audit_trail_integrity", "Audit-trail hash-chain integrity check", 7 * DAY, _job_audit_integrity),
]}


async def _last_success(db, job_id: str) -> datetime | None:
    from app.models.job_run import JobRun
    row = (
        await db.execute(
            select(JobRun.finishedAt).where(JobRun.jobId == job_id).where(JobRun.status == "SUCCESS")
            .order_by(JobRun.startedAt.desc()).limit(1)
        )
    ).first()
    return row[0] if row and row[0] else None


async def run_job(job_id: str, trigger: str = "MANUAL") -> dict[str, Any]:
    """Execute one job in its own session + JobRun record. Never raises."""
    from app.core.audit_context import set_system_actor
    from app.models.job_run import JobRun

    job = JOBS.get(job_id)
    if job is None:
        return {"error": f"unknown job {job_id}"}
    set_system_actor(job_id)
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    run_id = None
    async with AsyncSessionLocal() as db:
        jr = JobRun(jobId=job_id, startedAt=started, status="RUNNING", trigger=trigger)
        db.add(jr)
        await db.commit()
        run_id = jr.id
    try:
        async with AsyncSessionLocal() as db:
            summary = await job.fn(db)
            await db.commit()
        status, error = "SUCCESS", None
    except Exception as e:  # noqa: BLE001
        summary, status, error = {}, "FAILED", str(e)[:500]
        log.warning("job %s FAILED: %s", job_id, e)
    finally:
        async with AsyncSessionLocal() as db:
            jr = await db.get(JobRun, run_id)
            if jr:
                jr.status = status
                jr.finishedAt = datetime.now(timezone.utc).replace(tzinfo=None)
                jr.summary = summary
                jr.error = error
                jr.recordsAffected = next((summary.get(k) for k in ("written", "created", "updated", "flagged", "evaluated", "statusChanged") if isinstance(summary, dict) and summary.get(k) is not None), None)
                await db.commit()
    return {"jobId": job_id, "status": status, "summary": summary, "error": error}


async def supervisor_loop(stop: asyncio.Event) -> None:
    """Single supervisor — every 60s, run any job whose interval has elapsed since
    its last success. Staggered naturally by differing intervals."""
    next_due: dict[str, datetime] = {}
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        for jid, job in JOBS.items():
            last = await _last_success(db, jid)
            last_aware = (last.replace(tzinfo=timezone.utc) if last and last.tzinfo is None else last) if last else None
            next_due[jid] = (last_aware + timedelta(seconds=job.interval_seconds)) if last_aware else now
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        for jid, job in JOBS.items():
            if now >= next_due.get(jid, now):
                await run_job(jid, trigger="SCHEDULED")
                next_due[jid] = datetime.now(timezone.utc) + timedelta(seconds=job.interval_seconds)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
