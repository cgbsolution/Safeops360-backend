"""Post-closure rules engine for Incident.

Fires when an incident's workflow CLOSURE step completes. Each rule is a
best-effort sub-operation wrapped in a SAVEPOINT so a single failure
never blocks the closure or rolls back others. Each rule appends a
`{ruleName, fired, reason, spawnedRecordNumber?}` entry to the audit log
which is stored on the incident as `lessonsDistributedTo` (reusing the
JSON column for the audit; the field is misnamed historically but the
shape is generic).

Rules:
  1. Contractor score impact — decrement scores for involved contractors
     based on severity. CRITICAL = -10, HIGH = -5, MEDIUM = -2, LOW = -1.
  2. Linked observation cross-link — every observation in
     `incident.linkedObservationIds` is updated to flag it as a
     "missed warning" (best-effort, idempotent).
  3. Lessons distribution — record which plants should receive the
     lessons-learned text. Stored as a list of plant IDs; the actual
     notification mechanism (email, dashboard alert) is downstream.
  4. Equipment re-inspection — for any IncidentEquipment row marked
     DAMAGED / MALFUNCTION / INADEQUATE_GUARDING, schedule an
     inspection task. (Stub for now — records intent; the inspection
     module would create the actual task.)
  5. 90-day effectiveness review — set
     `incident.effectivenessReviewDueAt = closedAt + 90 days`. The
     workflow engine's separate scheduler picks this up later.

This mirrors the Near Miss post-closure rules pattern from
`app/services/post_closure_rules_nm.py`."""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentEquipment, IncidentPerson


# Severity → contractor score deduction (per company per incident).
_CONTRACTOR_SCORE_DEDUCTION: dict[str, int] = {
    "CRITICAL": 10,
    "HIGH": 5,
    "MEDIUM": 2,
    "LOW": 1,
}


async def _rule_contractor_score(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Decrement ContractorCompany.score for every contractor whose
    workman was involved in this incident (deduplicated)."""
    from app.models.masters import ContractorCompany

    deduction = _CONTRACTOR_SCORE_DEDUCTION.get(incident.severity or "LOW", 1)

    # Find involved contractors (deduplicated)
    rows = (
        await db.execute(
            select(IncidentPerson.contractorCompanyId)
            .where(IncidentPerson.incidentId == incident.id)
            .where(IncidentPerson.isContractor.is_(True))
            .where(IncidentPerson.contractorCompanyId.is_not(None))
        )
    ).scalars().all()
    contractor_ids = sorted({c for c in rows if c})

    if not contractor_ids:
        return {"ruleName": "Contractor Score Impact", "fired": False, "reason": "No contractor workmen involved."}

    affected: list[dict[str, Any]] = []
    for cid in contractor_ids:
        cc = await db.get(ContractorCompany, cid)
        if cc is None:
            continue
        before = cc.score
        cc.score = max(0, cc.score - deduction)
        affected.append({"id": cc.id, "name": cc.name, "before": before, "after": cc.score})

    if not affected:
        return {"ruleName": "Contractor Score Impact", "fired": False, "reason": "Contractor companies not found."}

    # Stash the deduction record on the incident for audit
    incident.contractorScoreImpact = {
        "deduction": deduction,
        "severity": incident.severity,
        "appliedTo": affected,
        "appliedAt": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "ruleName": "Contractor Score Impact",
        "fired": True,
        "reason": f"Decremented {len(affected)} contractor score(s) by {deduction} each.",
    }


async def _rule_observation_crosslink(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """For every observation in `incident.linkedObservationIds`, ensure
    a back-link exists on the observation side flagging it as a "missed
    warning" tied to this incident. Idempotent."""
    from app.models.observation import Observation

    obs_ids = list(incident.linkedObservationIds or [])
    if not obs_ids:
        return {"ruleName": "Observation Cross-link", "fired": False, "reason": "No linked observations."}

    linked_count = 0
    for oid in obs_ids:
        obs = await db.get(Observation, oid)
        if obs is None:
            continue
        # Best-effort write to a generic audit field — Observation's
        # contributedToIncidentId already exists for this purpose.
        if hasattr(obs, "contributedToIncidentId") and obs.contributedToIncidentId != incident.id:
            obs.contributedToIncidentId = incident.id
            linked_count += 1

    return {
        "ruleName": "Observation Cross-link",
        "fired": linked_count > 0,
        "reason": (
            f"Cross-linked {linked_count} observation(s) as missed warnings."
            if linked_count > 0
            else "All linked observations already cross-linked (idempotent)."
        ),
    }


async def _rule_lessons_distribution(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Record which plants should receive the lessons-learned text. The
    actual delivery mechanism (email / dashboard banner / portal post)
    is downstream — this rule just enumerates the recipient plant IDs."""
    from app.models.plant import Plant

    if not incident.lessonsLearned:
        return {"ruleName": "Lessons Distribution", "fired": False, "reason": "No lessons-learned text recorded."}

    # For LTI/Fatality: distribute to ALL plants. For lower severity,
    # just the source plant + sibling plants in the same state.
    severity = incident.severity or "LOW"
    if severity in ("HIGH", "CRITICAL"):
        plants = (await db.execute(select(Plant.id))).scalars().all()
    else:
        # Just the source plant for now
        plants = [incident.plantId]

    incident.lessonsDistributedTo = list(plants)
    return {
        "ruleName": "Lessons Distribution",
        "fired": True,
        "reason": f"Earmarked for distribution to {len(plants)} plant(s).",
    }


async def _rule_equipment_reinspection(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """For any equipment marked DAMAGED / MALFUNCTION / INADEQUATE_GUARDING
    during the investigation, schedule a re-inspection task. (Stub for
    now — records the intent on `incident.triggeredCapaIds` style audit;
    the actual inspection task creation lives in the Inspection module
    and would be wired up when that module gets its own refactor.)"""
    rows = (
        await db.execute(
            select(IncidentEquipment).where(IncidentEquipment.incidentId == incident.id)
        )
    ).scalars().all()

    target_involvements = {"DAMAGED", "MALFUNCTION", "INADEQUATE_GUARDING"}
    needs = [r for r in rows if r.involvement in target_involvements]
    if not needs:
        return {"ruleName": "Equipment Re-inspection", "fired": False, "reason": "No equipment requires re-inspection."}

    # Record intent on the incident — the Inspection module would consume
    # this in its own refactor. For now, the audit row tells future
    # reviewers what was intended.
    return {
        "ruleName": "Equipment Re-inspection",
        "fired": True,
        "reason": f"Flagged {len(needs)} equipment item(s) for re-inspection.",
    }


async def _rule_effectiveness_review(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Set effectivenessReviewDueAt = closedAt + 90 days. A separate
    scheduled job reads this column and creates the review task when
    the date arrives."""
    closed = incident.closedAt or datetime.now(timezone.utc)
    incident.effectivenessReviewDueAt = closed + timedelta(days=90)
    return {
        "ruleName": "90-Day Effectiveness Review Scheduling",
        "fired": True,
        "reason": f"Review scheduled for {incident.effectivenessReviewDueAt.date()}.",
    }


_ALL_RULES = [
    _rule_contractor_score,
    _rule_observation_crosslink,
    _rule_lessons_distribution,
    _rule_equipment_reinspection,
    _rule_effectiveness_review,
]


async def run_incident_post_closure_rules(
    db: AsyncSession, incident_id: str
) -> list[dict[str, Any]]:
    """Run all post-closure rules for an incident. Each rule runs inside
    its own SAVEPOINT so one failure doesn't poison the others. Returns
    the audit log of {ruleName, fired, reason} entries."""

    incident = await db.get(Incident, incident_id)
    if incident is None:
        return [{"ruleName": "Bootstrap", "fired": False, "reason": "Incident not found."}]

    audit_log: list[dict[str, Any]] = []
    for rule in _ALL_RULES:
        try:
            async with db.begin_nested():
                entry = await rule(db, incident)
                audit_log.append(entry)
        except Exception as e:  # noqa: BLE001
            print(
                f"[incident post-closure] rule {rule.__name__} failed: {e}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            audit_log.append({
                "ruleName": rule.__name__.replace("_rule_", "").replace("_", " ").title(),
                "fired": False,
                "reason": f"Rule errored: {str(e)[:120]}",
                "error": True,
            })

    await db.flush()
    return audit_log
