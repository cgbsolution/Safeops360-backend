"""Safety Culture Management — scoring engine + KRI wiring + recognition.

This is a live engine, not a static survey: ``calculate_culture_score`` recomputes
a site's maturity from five components (leadership engagement, worker participation,
leading/lagging ratio, BBS quality, perception), snapshots it monthly, and the
aggregate is fed into the ERM KRI framework as an auto-updating Key Risk Indicator
(see ``erm_metrics`` providers + ``register_culture_kris`` below).

Design principles honoured here:
  • Culture score is a shared aggregate per site (§0).
  • BBS is quality-weighted, not raw count, and gaming-resistant (§2).
  • Recognition points are quality-weighted only — never raw submissions (§6).
  • Severity weights / stage thresholds / targets are industry-configurable (§Cross-cutting).

Recalculation is async/background (the scheduler ``culture_recalc`` job), never a
synchronous blocking call on observation submission.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.safety_culture import (
    CultureMaturityProfile,
    CultureMaturitySnapshot,
    CultureObservationClosure,
    LeadershipWalk,
    PerceptionIndexSnapshot,
    PerceptionSurveyResponse,
    PerceptionSurveyTemplate,
    RecognitionEntry,
)

WINDOW_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ════════════════════════════════════════════════════════════════════════════
# Industry-configurable scoring parameters (§Cross-cutting: configurable per
# vertical, consistent with the checkpoint-library pattern). A vertical inherits
# _DEFAULT and overrides only what differs.
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CultureConfig:
    severity_weights: dict[str, int]
    # composite weights must sum to 1.0
    component_weights: dict[str, float]
    stage_thresholds: list[tuple[float, str]]  # (upper_inclusive, stage)
    leading_lagging_target: float  # leading:lagging ratio that scores 100
    participation_target_pct: float  # % of workforce engaged that scores 100
    observer_cap_pct: float  # no single observer > this share of period weighted score
    expected_weighted_floor: float  # min expected weighted observation points / 90d
    expected_per_capita: float  # + this * headcount
    min_survey_responses: int
    min_survey_rate_pct: float


_DEFAULT = CultureConfig(
    severity_weights={"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 5},
    component_weights={
        "leadershipEngagement": 0.30,
        "workerParticipation": 0.20,
        "leadingLaggingRatio": 0.20,
        "bbsQualityIndex": 0.20,
        "perceptionIndex": 0.10,
    },
    stage_thresholds=[(25, "Reactive"), (50, "Dependent"), (75, "Independent"), (100, "Interdependent")],
    leading_lagging_target=10.0,
    participation_target_pct=50.0,
    observer_cap_pct=0.15,
    expected_weighted_floor=25.0,
    expected_per_capita=1.5,
    min_survey_responses=10,
    min_survey_rate_pct=30.0,
)

# Per-vertical overrides. Higher-hazard verticals weight leading indicators and
# perception (trust-in-reporting) harder; garments weights worker participation.
_INDUSTRY_OVERRIDES: dict[str, dict[str, Any]] = {
    "Chemical": {"leading_lagging_target": 15.0, "severity_weights": {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 6}},
    "Pharma": {"leading_lagging_target": 12.0},
    "Cement": {"leading_lagging_target": 12.0},
    "Steel": {"leading_lagging_target": 12.0, "severity_weights": {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 6}},
    "Garments": {"participation_target_pct": 60.0, "leading_lagging_target": 8.0},
    "Food": {"leading_lagging_target": 8.0},
    "Automotive": {"leading_lagging_target": 10.0},
    "EPC": {"participation_target_pct": 45.0, "leading_lagging_target": 12.0},
}


def _match_vertical(raw: str | None) -> str | None:
    """FactoryProfile.primaryIndustry carries strings like 'Garments / Textile'
    or 'Bulk Drug / API'. Loosely map to an override key by substring."""
    if not raw:
        return None
    low = raw.lower()
    aliases = {
        "Chemical": ["chemical", "api", "bulk drug", "specialty"],
        "Pharma": ["pharma", "formulation", "drug"],
        "Cement": ["cement"],
        "Steel": ["steel", "metal", "foundry"],
        "Garments": ["garment", "textile", "apparel", "knit"],
        "Food": ["food", "beverage", "dairy", "fmcg"],
        "Automotive": ["auto", "automotive", "vehicle"],
        "EPC": ["epc", "construction", "engineering", "project"],
    }
    for key, needles in aliases.items():
        if any(n in low for n in needles):
            return key
    return None


def config_for(vertical: str | None) -> CultureConfig:
    key = _match_vertical(vertical)
    if key is None or key not in _INDUSTRY_OVERRIDES:
        return _DEFAULT
    ov = _INDUSTRY_OVERRIDES[key]
    base = _DEFAULT
    return CultureConfig(
        severity_weights=ov.get("severity_weights", base.severity_weights),
        component_weights=ov.get("component_weights", base.component_weights),
        stage_thresholds=ov.get("stage_thresholds", base.stage_thresholds),
        leading_lagging_target=ov.get("leading_lagging_target", base.leading_lagging_target),
        participation_target_pct=ov.get("participation_target_pct", base.participation_target_pct),
        observer_cap_pct=ov.get("observer_cap_pct", base.observer_cap_pct),
        expected_weighted_floor=ov.get("expected_weighted_floor", base.expected_weighted_floor),
        expected_per_capita=ov.get("expected_per_capita", base.expected_per_capita),
        min_survey_responses=ov.get("min_survey_responses", base.min_survey_responses),
        min_survey_rate_pct=ov.get("min_survey_rate_pct", base.min_survey_rate_pct),
    )


def stage_for(score: float, cfg: CultureConfig) -> str:
    for upper, stage in cfg.stage_thresholds:
        if score <= upper:
            return stage
    return cfg.stage_thresholds[-1][1]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ════════════════════════════════════════════════════════════════════════════
# Site context
# ════════════════════════════════════════════════════════════════════════════
async def _plant_headcount(db: AsyncSession, plant_id: str) -> int:
    from app.models.user import User

    n = (await db.execute(select(func.count()).select_from(User).where(User.plantId == plant_id))).scalar() or 0
    return int(n)


async def _vertical_for_plant(db: AsyncSession, plant_id: str) -> str | None:
    """Resolve the industry vertical from the Factory Profile Master (siteId == Plant.id)."""
    try:
        from app.models.factory import FactoryProfile

        v = (
            await db.execute(select(FactoryProfile.primaryIndustry).where(FactoryProfile.siteId == plant_id))
        ).scalar_one_or_none()
        return v
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
# §2 BBS Quality Index + gaming-pattern detection
# ════════════════════════════════════════════════════════════════════════════
async def _observations_window(db: AsyncSession, plant_id: str, since: datetime) -> list[Any]:
    from app.models.observation import Observation

    rows = (
        await db.execute(
            select(
                Observation.id, Observation.observerId, Observation.severity, Observation.category,
                Observation.status, Observation.capaId, Observation.description, Observation.date,
                Observation.createdAt,
            )
            .where(Observation.plantId == plant_id)
            .where(Observation.createdAt >= since)
        )
    ).all()
    return list(rows)


async def _closures_by_obs(db: AsyncSession, plant_id: str) -> dict[str, CultureObservationClosure]:
    rows = (
        await db.execute(select(CultureObservationClosure).where(CultureObservationClosure.plantId == plant_id))
    ).scalars().all()
    return {c.observationId: c for c in rows}


def _closure_multiplier(has_link: bool, verified: bool) -> float:
    if has_link and verified:
        return 1.5
    if has_link:
        return 1.0
    return 0.5


async def bbs_quality_index(db: AsyncSession, plant_id: str, cfg: CultureConfig | None = None) -> dict[str, Any]:
    """Quality-weighted BBS index (0-100). Replaces raw count as the headline
    metric (§2): Σ(severityWeight × closureLoopMultiplier), per-observer capped,
    over the expected weighted target."""
    cfg = cfg or _DEFAULT
    since = _now() - timedelta(days=WINDOW_DAYS)
    obs = await _observations_window(db, plant_id, since)
    closures = await _closures_by_obs(db, plant_id)

    per_observer: dict[str, float] = {}
    for o in obs:
        sev = o.severity.value if hasattr(o.severity, "value") else str(o.severity)
        weight = cfg.severity_weights.get(sev, 1)
        cl = closures.get(o.id)
        has_link = bool(o.capaId) or (cl is not None and (cl.linkedCapaId or cl.linkedActionId))
        verified = cl is not None and cl.reobservationVerified
        mult = _closure_multiplier(bool(has_link), bool(verified))
        per_observer[o.observerId] = per_observer.get(o.observerId, 0.0) + weight * mult

    raw_total = sum(per_observer.values())
    # Anti-skew cap: no single observer contributes > observer_cap_pct of the total.
    cap = raw_total * cfg.observer_cap_pct
    capped_total = sum(min(v, cap) for v in per_observer.values()) if raw_total > 0 else 0.0

    headcount = await _plant_headcount(db, plant_id)
    expected = max(cfg.expected_weighted_floor, headcount * cfg.expected_per_capita)
    index = _clamp(capped_total / expected * 100 if expected > 0 else 0.0)

    verified_closures = sum(1 for c in closures.values() if c.reobservationVerified)
    return {
        "bbsQualityIndex": round(index, 1),
        "observationCount": len(obs),
        "weightedTotal": round(raw_total, 1),
        "cappedWeightedTotal": round(capped_total, 1),
        "expectedTarget": round(expected, 1),
        "distinctObservers": len(per_observer),
        "verifiedClosures": verified_closures,
    }


async def integrity_flags(db: AsyncSession, plant_id: str, flag_n: int = 5) -> list[dict[str, Any]]:
    """Gaming-pattern detection (§2). Flags for human review (coaching, not
    punitive) — never auto-blocks. Patterns:
      • same observer + same category + same time-of-day recurring ≥ flag_n / period
      • low-effort: short/no detail + marked SAFE, exceeding a share of submissions
      • deadline spike: submissions concentrated in the last 48h of the period
    """
    from app.models.observation import Observation

    since = _now() - timedelta(days=30)
    rows = (
        await db.execute(
            select(
                Observation.id, Observation.observerId, Observation.type, Observation.category,
                Observation.description, Observation.createdAt,
            )
            .where(Observation.plantId == plant_id)
            .where(Observation.createdAt >= since)
        )
    ).all()

    # month period bounds for the deadline-spike test
    now = _now()
    period_end = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    # end of current month ≈ start of next
    nxt = period_end.replace(year=period_end.year + (period_end.month // 12), month=(period_end.month % 12) + 1)
    deadline_window_start = nxt - timedelta(hours=48)

    by_observer: dict[str, list[Any]] = {}
    for r in rows:
        by_observer.setdefault(r.observerId, []).append(r)

    flags: list[dict[str, Any]] = []
    for observer_id, items in by_observer.items():
        total = len(items)
        if total == 0:
            continue
        # pattern 1: same category + same hour-of-day recurrences
        combos: dict[tuple[str, int], int] = {}
        for r in items:
            cat = r.category.value if hasattr(r.category, "value") else str(r.category)
            hour = _aware(r.createdAt).hour if r.createdAt else 0
            combos[(cat, hour)] = combos.get((cat, hour), 0) + 1
        repeat = [(c, h, n) for (c, h), n in combos.items() if n >= flag_n]

        # pattern 2: low-effort (short/empty detail + "safe" type)
        low_effort = sum(
            1 for r in items
            if (not r.description or len(r.description.strip()) < 15)
            and (str(r.type.value if hasattr(r.type, "value") else r.type) in ("SAFE_ACT", "SAFE_CONDITION"))
        )
        low_effort_pct = low_effort / total * 100

        # pattern 3: deadline-driven spike
        deadline_spike = sum(1 for r in items if r.createdAt and _aware(r.createdAt) >= deadline_window_start)
        deadline_pct = deadline_spike / total * 100

        reasons: list[str] = []
        if repeat:
            reasons.append(
                f"{len(repeat)} category/time-of-day cluster(s) repeating ≥{flag_n}× "
                f"(e.g. {repeat[0][0]} @ {repeat[0][1]:02d}:00 ×{repeat[0][2]})"
            )
        if low_effort_pct >= 40 and low_effort >= 3:
            reasons.append(f"{low_effort_pct:.0f}% low-effort 'safe' entries with little/no detail")
        if deadline_pct >= 50 and deadline_spike >= 3:
            reasons.append(f"{deadline_pct:.0f}% of entries in the last 48h of the period (deadline-driven)")

        if reasons:
            flags.append({
                "observerId": observer_id,
                "totalSubmissions": total,
                "lowEffortPct": round(low_effort_pct, 0),
                "deadlineSpikePct": round(deadline_pct, 0),
                "patterns": reasons,
                "framing": "coaching",
            })
    flags.sort(key=lambda f: len(f["patterns"]), reverse=True)
    return flags


# ════════════════════════════════════════════════════════════════════════════
# §1 component: leading/lagging ratio + worker participation
# ════════════════════════════════════════════════════════════════════════════
async def _leading_lagging(db: AsyncSession, plant_id: str, cfg: CultureConfig) -> dict[str, Any]:
    from app.models.audit_compliance import ComplianceAudit
    from app.models.competency_matrix import CompetencyRecord
    from app.models.incident import Incident
    from app.models.near_miss import NearMiss
    from app.models.observation import Observation

    since = _now() - timedelta(days=WINDOW_DAYS)

    async def _count(model, date_col, extra=None):
        stmt = select(func.count()).select_from(model).where(model.plantId == plant_id).where(date_col >= since)
        if extra is not None:
            stmt = stmt.where(extra)
        return int((await db.execute(stmt)).scalar() or 0)

    obs_n = await _count(Observation, Observation.createdAt)
    nm_n = await _count(NearMiss, NearMiss.createdAt)
    audit_n = await _count(ComplianceAudit, ComplianceAudit.scheduledDate)
    training_n = await _count(CompetencyRecord, CompetencyRecord.createdAt, CompetencyRecord.state == "validated")
    leading = obs_n + nm_n + audit_n + training_n

    inc_n = await _count(Incident, Incident.createdAt)
    # LTI / MTC / RWC / fatal from Manhours over the same window (raw SQL — the
    # Manhours ORM columns are out of sync with the DB; erm_metrics does the same).
    lagging_mh = 0
    try:
        ey, em = _now().year, _now().month
        end_idx = ey * 12 + em
        start_idx = end_idx - 3  # ~90 days
        row = (
            await db.execute(
                text(
                    'SELECT COALESCE(SUM("ltiCount"),0)+COALESCE(SUM("mtcCount"),0)'
                    '+COALESCE(SUM("rwcCount"),0)+COALESCE(SUM("fatalityCount"),0) '
                    'FROM "Manhours" WHERE "plantId" = :p AND (year*12+month) > :s AND (year*12+month) <= :e'
                ),
                {"p": plant_id, "s": start_idx, "e": end_idx},
            )
        ).first()
        lagging_mh = int((row[0] if row else 0) or 0)
    except Exception:
        lagging_mh = 0

    lagging = inc_n + lagging_mh
    ratio = leading / max(1, lagging)
    score = _clamp(ratio / cfg.leading_lagging_target * 100)
    return {
        "score": round(score, 1),
        "leading": leading,
        "lagging": lagging,
        "ratio": round(ratio, 1),
        "breakdown": {"observations": obs_n, "nearMisses": nm_n, "audits": audit_n, "trainings": training_n, "incidents": inc_n, "injuries": lagging_mh},
    }


async def _worker_participation(db: AsyncSession, plant_id: str, cfg: CultureConfig) -> float:
    from app.models.near_miss import NearMiss
    from app.models.observation import Observation

    since = _now() - timedelta(days=WINDOW_DAYS)
    obs_people = (
        await db.execute(
            select(Observation.observerId).where(Observation.plantId == plant_id).where(Observation.createdAt >= since)
        )
    ).scalars().all()
    nm_people = (
        await db.execute(
            select(NearMiss.reporterId).where(NearMiss.plantId == plant_id).where(NearMiss.createdAt >= since)
        )
    ).scalars().all()
    walk_leaders = (
        await db.execute(
            select(LeadershipWalk.leaderId).where(LeadershipWalk.plantId == plant_id).where(LeadershipWalk.scheduledDate >= since)
        )
    ).scalars().all()
    engaged = {p for p in obs_people if p} | {p for p in nm_people if p} | {p for p in walk_leaders if p}
    headcount = await _plant_headcount(db, plant_id)
    if headcount <= 0:
        return 0.0
    participation_pct = len(engaged) / headcount * 100
    return round(_clamp(participation_pct / cfg.participation_target_pct * 100), 1)


# ════════════════════════════════════════════════════════════════════════════
# §3 Leadership engagement (site aggregate)
# ════════════════════════════════════════════════════════════════════════════
async def leadership_compliance(db: AsyncSession, plant_id: str) -> dict[str, Any]:
    """Site-level compliance-to-schedule + blended engagement score over the window."""
    since = _now() - timedelta(days=WINDOW_DAYS)
    walks = (
        await db.execute(
            select(LeadershipWalk).where(LeadershipWalk.plantId == plant_id).where(LeadershipWalk.scheduledDate >= since)
        )
    ).scalars().all()
    scheduled = len(walks)
    completed = sum(1 for w in walks if w.status == "Completed")
    compliance = (completed / scheduled * 100) if scheduled else 0.0

    # walk quality: workersInteracted + hazardsIdentified + observationsRaised,
    # normalised against a simple benchmark (per completed walk).
    if completed:
        done = [w for w in walks if w.status == "Completed"]
        avg_signal = sum((w.workersInteracted + w.hazardsIdentified + w.observationsRaised) for w in done) / completed
        quality = _clamp(avg_signal / 12.0 * 100)  # benchmark: ~12 combined signals = full marks
    else:
        quality = 0.0
    engagement = round(_clamp(compliance * 0.6 + quality * 0.4), 1)
    return {
        "complianceToSchedule": round(compliance, 1),
        "engagementScore": engagement,
        "walkQuality": round(quality, 1),
        "scheduledWalks": scheduled,
        "completedWalks": completed,
    }


async def leader_scorecard(db: AsyncSession, leader_id: str) -> dict[str, Any]:
    since = _now() - timedelta(days=WINDOW_DAYS)
    walks = (
        await db.execute(
            select(LeadershipWalk).where(LeadershipWalk.leaderId == leader_id).where(LeadershipWalk.scheduledDate >= since)
            .order_by(LeadershipWalk.scheduledDate.desc())
        )
    ).scalars().all()
    scheduled = len(walks)
    completed = sum(1 for w in walks if w.status == "Completed")
    compliance = (completed / scheduled * 100) if scheduled else 0.0
    hazards = sum(w.hazardsIdentified for w in walks)
    workers = sum(w.workersInteracted for w in walks)
    obs = sum(w.observationsRaised for w in walks)
    quality = _clamp((workers + hazards + obs) / max(1, completed) / 12.0 * 100) if completed else 0.0
    return {
        "leaderId": leader_id,
        "scheduledWalks": scheduled,
        "completedWalks": completed,
        "complianceToSchedule": round(compliance, 1),
        "hazardsIdentified": hazards,
        "workersInteracted": workers,
        "observationsRaised": obs,
        "rollingEngagementScore": round(_clamp(compliance * 0.6 + quality * 0.4), 1),
        "recentWalks": [
            {
                "id": w.id, "scheduledDate": _aware(w.scheduledDate).isoformat() if w.scheduledDate else None,
                "completedDate": _aware(w.completedDate).isoformat() if w.completedDate else None,
                "status": w.status, "areaVisited": w.areaVisited,
                "hazardsIdentified": w.hazardsIdentified, "workersInteracted": w.workersInteracted,
            }
            for w in walks[:20]
        ],
    }


# ════════════════════════════════════════════════════════════════════════════
# §4 Perception survey index
# ════════════════════════════════════════════════════════════════════════════
_DIMENSIONS = ["TrustInReporting", "PsychologicalSafety", "ManagementCommitment", "PeerAccountability"]


def anonymous_token(user_id: str, salt: str = "safeops-perception") -> str:
    """One-way hash → prevents double-submit within a period without storing PII.
    Not reversible to a user identity."""
    return hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()


async def compute_perception_index(
    db: AsyncSession, plant_id: str, period: str, cfg: CultureConfig | None = None, publish: bool = True
) -> dict[str, Any]:
    cfg = cfg or _DEFAULT
    responses = (
        await db.execute(
            select(PerceptionSurveyResponse)
            .where(PerceptionSurveyResponse.plantId == plant_id)
            .where(PerceptionSurveyResponse.period == period)
        )
    ).scalars().all()
    count = len(responses)
    headcount = await _plant_headcount(db, plant_id)
    rate = (count / headcount * 100) if headcount else 0.0
    threshold_met = count >= cfg.min_survey_responses or rate >= cfg.min_survey_rate_pct

    # question → dimension map from the active template(s)
    q_dimension: dict[str, str] = {}
    templates = (await db.execute(select(PerceptionSurveyTemplate))).scalars().all()
    for t in templates:
        for q in (t.questions or []):
            if q.get("id"):
                q_dimension[q["id"]] = q.get("dimension", "")

    sums: dict[str, list[int]] = {d: [] for d in _DIMENSIONS}
    for r in responses:
        for ans in (r.responses or []):
            dim = q_dimension.get(ans.get("questionId", ""), "")
            score = ans.get("score")
            if dim in sums and isinstance(score, (int, float)):
                sums[dim].append(int(score))

    def _likert_to_100(vals: list[int]) -> float:
        if not vals:
            return 0.0
        avg = sum(vals) / len(vals)  # 1..5
        return round(_clamp((avg - 1) / 4 * 100), 1)

    dimension_scores = {
        "trustInReporting": _likert_to_100(sums["TrustInReporting"]),
        "psychologicalSafety": _likert_to_100(sums["PsychologicalSafety"]),
        "managementCommitment": _likert_to_100(sums["ManagementCommitment"]),
        "peerAccountability": _likert_to_100(sums["PeerAccountability"]),
    }
    present = [v for v in dimension_scores.values() if v > 0]
    composite = round(sum(present) / len(present), 1) if present else 0.0

    result = {
        "plantId": plant_id, "period": period, "dimensionScores": dimension_scores,
        "compositeScore": composite, "responseCount": count,
        "responseRatePercent": round(rate, 1), "thresholdMet": threshold_met,
    }

    if publish and threshold_met:
        existing = (
            await db.execute(
                select(PerceptionIndexSnapshot)
                .where(PerceptionIndexSnapshot.plantId == plant_id)
                .where(PerceptionIndexSnapshot.period == period)
            )
        ).scalar_one_or_none()
        if existing:
            existing.dimensionScores = dimension_scores
            existing.compositeScore = composite
            existing.responseCount = count
            existing.responseRatePercent = round(rate, 1)
            existing.thresholdMet = threshold_met
        else:
            db.add(PerceptionIndexSnapshot(
                plantId=plant_id, period=period, dimensionScores=dimension_scores,
                compositeScore=composite, responseCount=count, responseRatePercent=round(rate, 1),
                thresholdMet=threshold_met,
            ))
        await db.flush()
    return result


async def _latest_perception_composite(db: AsyncSession, plant_id: str) -> float:
    snap = (
        await db.execute(
            select(PerceptionIndexSnapshot)
            .where(PerceptionIndexSnapshot.plantId == plant_id)
            .where(PerceptionIndexSnapshot.thresholdMet.is_(True))
            .order_by(PerceptionIndexSnapshot.period.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return float(snap.compositeScore) if snap else 0.0


# ════════════════════════════════════════════════════════════════════════════
# §1 Culture Maturity Engine — the aggregate everything feeds
# ════════════════════════════════════════════════════════════════════════════
def _period_label(dt: datetime | None = None) -> str:
    dt = dt or _now()
    return dt.strftime("%Y-%m")


async def calculate_culture_score(db: AsyncSession, plant_id: str) -> CultureMaturityProfile:
    """Recompute the five components, the composite stage score, upsert the live
    profile, and snapshot the month. Caller commits."""
    vertical = await _vertical_for_plant(db, plant_id)
    cfg = config_for(vertical)

    bbs = await bbs_quality_index(db, plant_id, cfg)
    ll = await _leading_lagging(db, plant_id, cfg)
    participation = await _worker_participation(db, plant_id, cfg)
    leadership = await leadership_compliance(db, plant_id)
    perception = await _latest_perception_composite(db, plant_id)

    components = {
        "leadershipEngagement": leadership["engagementScore"],
        "workerParticipation": participation,
        "leadingLaggingRatio": ll["score"],
        "bbsQualityIndex": bbs["bbsQualityIndex"],
        "perceptionIndex": perception,
    }
    w = cfg.component_weights
    stage_score = round(sum(components[k] * w[k] for k in components), 1)
    stage = stage_for(stage_score, cfg)

    profile = (
        await db.execute(select(CultureMaturityProfile).where(CultureMaturityProfile.plantId == plant_id))
    ).scalar_one_or_none()
    if profile is None:
        profile = CultureMaturityProfile(plantId=plant_id)
        db.add(profile)
    profile.currentStage = stage
    profile.stageScore = stage_score
    profile.leadershipEngagement = components["leadershipEngagement"]
    profile.workerParticipation = components["workerParticipation"]
    profile.leadingLaggingRatio = components["leadingLaggingRatio"]
    profile.bbsQualityIndex = components["bbsQualityIndex"]
    profile.perceptionIndex = components["perceptionIndex"]
    profile.industryVertical = vertical
    profile.lastCalculatedAt = _now()
    await db.flush()

    # Monthly snapshot (upsert on plant+period).
    period = _period_label()
    snap = (
        await db.execute(
            select(CultureMaturitySnapshot)
            .where(CultureMaturitySnapshot.plantId == plant_id)
            .where(CultureMaturitySnapshot.period == period)
        )
    ).scalar_one_or_none()
    if snap is None:
        db.add(CultureMaturitySnapshot(
            plantId=plant_id, period=period, stageScore=stage_score, currentStage=stage, componentScores=components,
        ))
    else:
        snap.stageScore = stage_score
        snap.currentStage = stage
        snap.componentScores = components
    await db.flush()
    return profile


async def recalculate_all(db: AsyncSession) -> dict[str, Any]:
    """Scheduler entry point — recompute every plant's culture score, then award
    recognition for the current period. Runs as an async background job."""
    from app.models.plant import Plant

    plant_ids = (await db.execute(select(Plant.id))).scalars().all()
    period = _period_label()
    recalculated = 0
    awarded = 0
    for pid in plant_ids:
        try:
            await calculate_culture_score(db, pid)
            recalculated += 1
            res = await award_recognition(db, pid, period)
            awarded += res.get("awarded", 0)
        except Exception:
            continue
    await db.commit()
    return {"plantsRecalculated": recalculated, "recognitionAwarded": awarded, "period": period}


async def maturity_profile_out(db: AsyncSession, plant_id: str, with_history: bool = True) -> dict[str, Any]:
    profile = (
        await db.execute(select(CultureMaturityProfile).where(CultureMaturityProfile.plantId == plant_id))
    ).scalar_one_or_none()
    history: list[dict[str, Any]] = []
    if with_history:
        snaps = (
            await db.execute(
                select(CultureMaturitySnapshot).where(CultureMaturitySnapshot.plantId == plant_id)
                .order_by(CultureMaturitySnapshot.period.asc()).limit(24)
            )
        ).scalars().all()
        history = [
            {"period": s.period, "stageScore": s.stageScore, "currentStage": s.currentStage, "componentScores": s.componentScores}
            for s in snaps
        ]
    if profile is None:
        return {"plantId": plant_id, "currentStage": "Reactive", "stageScore": 0.0, "componentScores": {}, "history": history, "lastCalculatedAt": None}
    return {
        "plantId": plant_id,
        "currentStage": profile.currentStage,
        "stageScore": profile.stageScore,
        "industryVertical": profile.industryVertical,
        "componentScores": {
            "leadershipEngagement": profile.leadershipEngagement,
            "workerParticipation": profile.workerParticipation,
            "leadingLaggingRatio": profile.leadingLaggingRatio,
            "bbsQualityIndex": profile.bbsQualityIndex,
            "perceptionIndex": profile.perceptionIndex,
        },
        "history": history,
        "lastCalculatedAt": _aware(profile.lastCalculatedAt).isoformat() if profile.lastCalculatedAt else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# §6 Recognition Layer (quality-weighted only)
# ════════════════════════════════════════════════════════════════════════════
async def _quality_verified_streak_weeks(db: AsyncSession, plant_id: str, user_id: str) -> int:
    """Consecutive ISO weeks (ending this week) with ≥1 quality-verified observation."""
    from app.models.observation import Observation

    since = _now() - timedelta(days=WINDOW_DAYS + 7)
    obs = (
        await db.execute(
            select(Observation.id, Observation.createdAt)
            .where(Observation.plantId == plant_id).where(Observation.observerId == user_id)
            .where(Observation.createdAt >= since)
        )
    ).all()
    if not obs:
        return 0
    closures = await _closures_by_obs(db, plant_id)
    verified_weeks: set[tuple[int, int]] = set()
    for o in obs:
        cl = closures.get(o.id)
        verified = (cl is not None and cl.reobservationVerified)
        if verified and o.createdAt:
            iso = _aware(o.createdAt).isocalendar()
            verified_weeks.add((iso[0], iso[1]))
    if not verified_weeks:
        return 0
    this = _now().isocalendar()
    year, week = this[0], this[1]
    streak = 0
    while (year, week) in verified_weeks:
        streak += 1
        week -= 1
        if week < 1:
            year -= 1
            week = 52
    return streak


async def _upsert_recognition(
    db: AsyncSession, plant_id: str, user_id: str, category: str, period: str,
    points: float, badge: str | None = None, streak_weeks: int = 0, detail: str | None = None,
) -> bool:
    existing = (
        await db.execute(
            select(RecognitionEntry)
            .where(RecognitionEntry.plantId == plant_id).where(RecognitionEntry.userId == user_id)
            .where(RecognitionEntry.category == category).where(RecognitionEntry.periodEarned == period)
        )
    ).scalar_one_or_none()
    if existing:
        existing.points = round(points, 1)
        existing.badgeAwarded = badge
        existing.streakWeeks = streak_weeks
        existing.detail = detail
        return False
    db.add(RecognitionEntry(
        plantId=plant_id, userId=user_id, category=category, periodEarned=period,
        points=round(points, 1), badgeAwarded=badge, streakWeeks=streak_weeks, detail=detail,
    ))
    return True


async def award_recognition(db: AsyncSession, plant_id: str, period: str) -> dict[str, Any]:
    """System-triggered (never manual). Points from quality-weighted contributions
    only: BBS quality contribution, verified closure-loop completions, leadership
    walk compliance, and quality-verified streaks. Caller commits (recalculate_all
    does; the API endpoint commits too)."""
    from app.models.observation import Observation

    cfg = config_for(await _vertical_for_plant(db, plant_id))
    awarded = 0
    since = _now() - timedelta(days=WINDOW_DAYS)
    closures = await _closures_by_obs(db, plant_id)

    # 1) QualityContribution — per-observer capped weighted BBS contribution
    obs = (
        await db.execute(
            select(Observation.id, Observation.observerId, Observation.severity, Observation.capaId)
            .where(Observation.plantId == plant_id).where(Observation.createdAt >= since)
        )
    ).all()
    per_observer: dict[str, float] = {}
    for o in obs:
        sev = o.severity.value if hasattr(o.severity, "value") else str(o.severity)
        weight = cfg.severity_weights.get(sev, 1)
        cl = closures.get(o.id)
        has_link = bool(o.capaId) or (cl is not None and (cl.linkedCapaId or cl.linkedActionId))
        verified = cl is not None and cl.reobservationVerified
        per_observer[o.observerId] = per_observer.get(o.observerId, 0.0) + weight * _closure_multiplier(bool(has_link), bool(verified))
    for uid, contrib in per_observer.items():
        if contrib <= 0:
            continue
        streak = await _quality_verified_streak_weeks(db, plant_id, uid)
        badge = "Quality Champion" if contrib >= 15 else None
        if await _upsert_recognition(db, plant_id, uid, "QualityContribution", period, round(contrib, 1), badge, detail="Quality-weighted BBS contribution"):
            awarded += 1
        # 2) ObservationStreak — quality-verified consecutive weeks
        if streak >= 2:
            sbadge = "🔥 On Fire" if streak >= 6 else "Streak"
            if await _upsert_recognition(db, plant_id, uid, "ObservationStreak", period, float(streak * 5), sbadge, streak_weeks=streak, detail=f"{streak} consecutive weeks with a quality-verified observation"):
                awarded += 1

    # 3) LeadershipWalkCompliance — leaders meeting schedule
    walk_leaders = (
        await db.execute(select(LeadershipWalk.leaderId).where(LeadershipWalk.plantId == plant_id).where(LeadershipWalk.scheduledDate >= since).distinct())
    ).scalars().all()
    for lid in walk_leaders:
        card = await leader_scorecard(db, lid)
        if card["scheduledWalks"] and card["complianceToSchedule"] >= 80:
            pts = round(card["complianceToSchedule"] / 2 + card["hazardsIdentified"] * 2, 1)
            badge = "Felt Leadership" if card["complianceToSchedule"] >= 95 else None
            if await _upsert_recognition(db, plant_id, lid, "LeadershipWalkCompliance", period, pts, badge, detail=f"{card['complianceToSchedule']:.0f}% walk compliance"):
                awarded += 1

    await db.flush()
    return {"plantId": plant_id, "period": period, "awarded": awarded}


async def leaderboard(db: AsyncSession, plant_id: str, period: str) -> dict[str, Any]:
    """Top performers + most-improved only (no bottom-of-board call-outs, §6)."""
    rows = (
        await db.execute(
            select(RecognitionEntry).where(RecognitionEntry.plantId == plant_id).where(RecognitionEntry.periodEarned == period)
        )
    ).scalars().all()
    totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = totals.setdefault(r.userId, {"userId": r.userId, "points": 0.0, "badges": [], "streakWeeks": 0})
        t["points"] += r.points
        if r.badgeAwarded:
            t["badges"].append(r.badgeAwarded)
        t["streakWeeks"] = max(t["streakWeeks"], r.streakWeeks)

    # most-improved: delta vs previous period total
    prev = _prev_period(period)
    prev_rows = (
        await db.execute(
            select(RecognitionEntry.userId, func.sum(RecognitionEntry.points))
            .where(RecognitionEntry.plantId == plant_id).where(RecognitionEntry.periodEarned == prev)
            .group_by(RecognitionEntry.userId)
        )
    ).all()
    prev_totals = {uid: float(p or 0) for uid, p in prev_rows}

    ranked = sorted(totals.values(), key=lambda x: x["points"], reverse=True)
    for i, t in enumerate(ranked):
        t["rank"] = i + 1
        t["points"] = round(t["points"], 1)

    improved = []
    for t in totals.values():
        delta = t["points"] - prev_totals.get(t["userId"], 0.0)
        if delta > 0:
            improved.append({"userId": t["userId"], "delta": round(delta, 1), "points": t["points"]})
    improved.sort(key=lambda x: x["delta"], reverse=True)

    return {"plantId": plant_id, "period": period, "individual": ranked[:15], "mostImproved": improved[:5]}


def _prev_period(period: str) -> str:
    try:
        y, m = period.split("-")
        y, m = int(y), int(m)
        m -= 1
        if m < 1:
            y -= 1
            m = 12
        return f"{y:04d}-{m:02d}"
    except Exception:
        return period


async def user_streaks(db: AsyncSession, user_id: str) -> dict[str, Any]:
    from app.models.user import User

    user = await db.get(User, user_id)
    plant_id = user.plantId if user else None
    streak = await _quality_verified_streak_weeks(db, plant_id, user_id) if plant_id else 0
    entries = (
        await db.execute(
            select(RecognitionEntry).where(RecognitionEntry.userId == user_id).order_by(RecognitionEntry.periodEarned.desc()).limit(12)
        )
    ).scalars().all()
    total_points = round(sum(e.points for e in entries), 1)
    badges = sorted({e.badgeAwarded for e in entries if e.badgeAwarded})
    return {
        "userId": user_id, "currentStreakWeeks": streak, "totalPoints": total_points, "badges": badges,
        "history": [{"period": e.periodEarned, "category": e.category, "points": e.points, "badge": e.badgeAwarded} for e in entries],
    }


# ════════════════════════════════════════════════════════════════════════════
# §5 ERM / KRI integration — the structural differentiator
# ════════════════════════════════════════════════════════════════════════════
_CULTURE_KRIS = [
    # (kriCode, name, metricProviderKey, thresholdGreen, thresholdAmber)
    # All LOWER_IS_WORSE (a low score = worse culture). GREEN ≥ green, RED < amber.
    ("KRI-CULT-MATURITY", "Safety Culture Maturity Score", "culture.maturity_score", 60.0, 40.0),
    ("KRI-CULT-BBS", "BBS Observation Quality Index", "culture.bbs_quality", 55.0, 35.0),
    ("KRI-CULT-LEADERSHIP", "Leadership Engagement — walk compliance", "culture.leadership_compliance", 80.0, 60.0),
    ("KRI-CULT-PERCEPTION", "Worker Safety Perception Index", "culture.perception_composite", 65.0, 45.0),
]


async def register_culture_kris(db: AsyncSession, actor_id: str | None = None) -> dict[str, Any]:
    """One-time wiring (§5): ensure the 'Human Factor / Safety Culture Risk'
    register entry and register the four culture scores as auto-updating
    MODULE_FED KRIs against it. Idempotent. The existing hourly kri_module_feeds
    job then keeps them live — no polling, no manual entry. Caller/endpoint commits."""
    from app.models.erm import EnterpriseRisk, RiskCategory
    from app.models.erm_p2 import KriDefinition
    from app.models.user import Role, User, UserRole

    # 1) Risk category
    cat = (await db.execute(select(RiskCategory).where(RiskCategory.code == "HUMAN_FACTOR"))).scalar_one_or_none()
    if cat is None:
        cat = RiskCategory(
            code="HUMAN_FACTOR", name="Human Factor / Safety Culture",
            description="People, behaviour and safety-culture maturity risk.",
            colorHex="#C9A961", displayOrder=90, isSystemCategory=True, isActive=True,
        )
        db.add(cat)
        await db.flush()

    # 2) Owner — first CRO / HSE_MANAGER / any user, falling back to actor
    owner_id = actor_id
    owner = (
        await db.execute(
            select(User.id).join(UserRole, UserRole.userId == User.id).join(Role, Role.id == UserRole.roleId)
            .where(Role.code.in_(("CRO", "HSE_MANAGER", "CORPORATE_HSE"))).limit(1)
        )
    ).scalar_one_or_none()
    owner_id = owner or actor_id or (await db.execute(select(User.id).limit(1))).scalar_one_or_none()

    # 3) Enterprise risk register entry
    risk = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.riskCode == "ERM-HUMANFACTOR"))).scalar_one_or_none()
    if risk is None and owner_id:
        risk = EnterpriseRisk(
            riskCode="ERM-HUMANFACTOR", title="Human Factor / Safety Culture Risk",
            description="Risk that an immature or declining safety culture (weak felt leadership, "
                        "low-quality reporting, poor trust-in-reporting) precedes incidents. Monitored live "
                        "via culture KRIs fed from the Safety Culture Management module.",
            categoryId=cat.id, orgLevel="ENTERPRISE", riskOwnerId=owner_id, riskChampionId=owner_id,
            lifecycleState="ACTIVE", sourceType="MODULE_FED",
            identifiedDate=_now(), nextReviewDate=_now() + timedelta(days=90),
            inherentLikelihood=4, inherentImpact=4, inherentScore=16, inherentBand="High",
            residualLikelihood=3, residualImpact=4, residualScore=12, residualBand="High",
        )
        db.add(risk)
        await db.flush()

    linked = [risk.id] if risk else []

    # 4) The four KRIs
    created = updated = 0
    for code, name, provider_key, green, amber in _CULTURE_KRIS:
        kri = (await db.execute(select(KriDefinition).where(KriDefinition.kriCode == code))).scalar_one_or_none()
        if kri is None:
            db.add(KriDefinition(
                kriCode=code, name=name, description=f"Auto-fed from the Safety Culture module ({provider_key}).",
                categoryId=cat.id, linkedRiskIds=linked, unit="score (0-100)", direction="LOWER_IS_WORSE",
                indicatorType="LEADING", frequency="MONTHLY", feedType="MODULE_FED", metricProviderKey=provider_key,
                thresholdGreen=green, thresholdAmber=amber, ownerId=owner_id or "SYSTEM", isActive=True,
                createdBy=actor_id,
            ))
            created += 1
        else:
            kri.feedType = "MODULE_FED"
            kri.metricProviderKey = provider_key
            kri.linkedRiskIds = linked or kri.linkedRiskIds
            kri.categoryId = cat.id
            kri.isActive = True
            updated += 1
    await db.flush()
    return {"riskCode": "ERM-HUMANFACTOR", "categoryCode": "HUMAN_FACTOR", "krisCreated": created, "krisUpdated": updated, "linkedRiskId": risk.id if risk else None}
