"""Safety Observation insight rules (spec §2.2) — deterministic.

Bar:
  * cluster — open unsafe acts/conditions concentrating in one category at a
    plant (≥3 sharing category), e.g. electrical unsafe acts up at a plant.
  * duplicate — near-identical descriptions in the same plant/area logged within
    48h (fuzzy token overlap). Catches copy-paste / test-data rows for cleanup.
Row signals (0-1 per record, highest-priority wins):
  * duplicate "Likely duplicate" — the later member of a duplicate pair.
  * anomaly "Check severity" — HIGH/CRITICAL severity on a one-line, vague
    description (flags for a second look; never overrides the human severity).
  * next_best_action "Escalate" — sat OPEN/ASSIGNED past the review SLA.

Every count traces to a field counted below; no model calls. Clustering and
duplicate detection are plain Python token math (common.keywords).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.schemas.insights import Insight, Signal
from app.services.insights.common import (
    age_days,
    as_naive,
    confidence_for,
    keywords,
    now_naive,
    refs_str,
)
from app.services.insights.templates import fill

_WINDOW_DAYS = 180
_DUP_WINDOW_HOURS = 48
_DUP_JACCARD = 0.6
_ESCALATE_SLA_DAYS = 7
_VAGUE_WORD_MAX = 3  # a HIGH/CRITICAL entry described in ≤3 words is suspect


def _humanize(token: str) -> str:
    return token.replace("_", " ").title()


def _slug(*parts: str) -> str:
    return ":".join(p.lower().replace(" ", "-")[:24] for p in parts if p)


async def compute_observation(
    db: AsyncSession,
    *,
    plant: str | None,
    date_from: Any = None,  # reserved — filter parity with the list view
    date_to: Any = None,
) -> tuple[list[Insight], list[Signal], int]:
    now = now_naive().replace(microsecond=0)
    window_start = now - timedelta(days=_WINDOW_DAYS)

    stmt = (
        select(
            Observation.id,
            Observation.number,
            Observation.date,
            Observation.plantId,
            Observation.areaId,
            cast(Observation.type, String).label("type"),
            cast(Observation.category, String).label("category"),
            cast(Observation.severity, String).label("severity"),
            cast(Observation.status, String).label("status"),
            Observation.description,
            Observation.createdAt,
        )
        .where(Observation.date >= window_start)
        .order_by(Observation.date.desc())
        .limit(600)
    )
    if plant:
        stmt = stmt.where(Observation.plantId == plant)
    rows = (await db.execute(stmt)).all()
    record_count = len(rows)
    if not rows:
        return [], [], 0

    plant_names: dict[str, str] = dict(
        (await db.execute(text('SELECT id, name FROM "Plant"'))).all()
    )

    open_rows = [r for r in rows if (r.status or "") != "CLOSED"]

    # Duplicate detection first — its result feeds both the bar card and the
    # per-row chips (so we don't double-flag).
    dup_groups = _duplicate_groups(rows)
    dup_ids = {rid for group in dup_groups for rid in group}

    bar: list[Insight] = []
    bar.extend(_cluster_insights(open_rows, plant_names))
    dup_card = _duplicate_insight(dup_groups, rows)
    if dup_card:
        bar.append(dup_card)

    signals = _row_signals(open_rows, dup_ids, now)
    return bar, signals, record_count


def _cluster_insights(open_rows: list[Any], plant_names: dict[str, str]) -> list[Insight]:
    """Open UNSAFE observations concentrating in one category at a plant."""
    unsafe = [r for r in open_rows if (r.type or "").startswith("UNSAFE")]
    by_plant: dict[str, list[Any]] = {}
    for r in unsafe:
        by_plant.setdefault(r.plantId, []).append(r)

    out: list[Insight] = []
    for plant_id, group in by_plant.items():
        if len(group) < 3:
            continue
        by_cat: dict[str, list[str]] = {}
        for r in group:
            by_cat.setdefault(r.category or "OTHER", []).append(r.number)
        cat, refs = max(by_cat.items(), key=lambda kv: len(kv[1]))
        count, total = len(refs), len(group)
        if count < 3:
            continue
        plant_label = plant_names.get(plant_id, "this plant")
        cat_label = _humanize(cat)
        out.append(
            Insight(
                id=_slug("observation", "cluster", plant_id, cat),
                kind="cluster",
                severity="high" if count >= 5 else "watch",
                headline=fill(
                    "observation.cluster.category",
                    count=count,
                    total=total,
                    category=cat_label,
                    plant=plant_label,
                ),
                evidence=fill(
                    "observation.cluster.category.evidence",
                    count=count,
                    category=cat_label,
                    plant=plant_label,
                    refs=refs_str(refs),
                ),
                recordRefs=refs,
                suggestedAction="Target a toolbox talk / focused inspection on this hazard category at this plant.",
                confidence=confidence_for(count),
            )
        )
    return out


def _token_set(text_value: str | None) -> set[str]:
    return set(keywords(text_value or ""))


def _duplicate_groups(rows: list[Any]) -> list[list[str]]:
    """Groups of record-ids that look like duplicates: same plant+area, logged
    within 48h, with high token overlap in the description."""
    # Bucket by (plant, area) to keep the O(n²) comparison local and cheap.
    buckets: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        buckets.setdefault((r.plantId, r.areaId or ""), []).append(r)

    groups: list[list[str]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        toks = {r.id: _token_set(r.description) for r in bucket}
        used: set[str] = set()
        for i, a in enumerate(bucket):
            if a.id in used:
                continue
            group = [a.id]
            for b in bucket[i + 1 :]:
                if b.id in used:
                    continue
                da, db_ = as_naive(a.date), as_naive(b.date)
                if da is None or db_ is None:
                    continue
                if abs((da - db_).total_seconds()) > _DUP_WINDOW_HOURS * 3600:
                    continue
                ta, tb = toks[a.id], toks[b.id]
                if not ta or not tb:
                    # Both effectively empty/vague descriptions in the same
                    # place within 48h — treat as duplicate candidates too.
                    if not ta and not tb:
                        group.append(b.id)
                        used.add(b.id)
                    continue
                inter = len(ta & tb)
                union = len(ta | tb)
                if union and inter / union >= _DUP_JACCARD:
                    group.append(b.id)
                    used.add(b.id)
            if len(group) > 1:
                used.update(group)
                groups.append(group)
    return groups


def _duplicate_insight(dup_groups: list[list[str]], rows: list[Any]) -> Insight | None:
    if not dup_groups:
        return None
    by_id = {r.id: r for r in rows}
    # Refs = every record involved in any duplicate group.
    refs = [by_id[rid].number for group in dup_groups for rid in group if rid in by_id]
    if len(refs) < 2:
        return None
    n_groups = len(dup_groups)
    return Insight(
        id="observation:duplicate:near-identical",
        kind="duplicate",
        severity="watch",
        headline=fill("observation.duplicate", groups=n_groups, records=len(refs)),
        evidence=fill("observation.duplicate.evidence", records=len(refs), refs=refs_str(refs)),
        recordRefs=refs,
        suggestedAction="Review these near-identical entries — merge or delete the duplicates to keep the register clean.",
        confidence=confidence_for(len(refs)),
    )


def _row_signals(open_rows: list[Any], dup_ids: set[str], now: Any) -> list[Signal]:
    out: list[Signal] = []
    for r in open_rows:
        sev = (r.severity or "").upper()
        word_count = len((r.description or "").split())

        if r.id in dup_ids:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="duplicate",
                    severity="watch",
                    label=fill("signal.duplicate.label"),
                    evidence=fill("signal.duplicate.evidence", ref=r.number),
                    suggestedAction="Confirm against the matching entry and remove the duplicate.",
                )
            )
        elif sev in {"HIGH", "CRITICAL"} and word_count <= _VAGUE_WORD_MAX:
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="anomaly",
                    severity="watch",
                    label=fill("signal.severity_mismatch.label"),
                    evidence=fill(
                        "signal.severity_mismatch.evidence", ref=r.number, severity=sev
                    ),
                    suggestedAction="Add detail or re-check the severity — the description is too thin to justify it.",
                )
            )
        elif (r.status or "") in {"OPEN", "ASSIGNED"} and (age_days(r.createdAt) or 0) > _ESCALATE_SLA_DAYS:
            days = age_days(r.createdAt) or 0
            out.append(
                Signal(
                    recordId=r.id,
                    recordRef=r.number,
                    kind="next_best_action",
                    severity="watch",
                    label=fill("signal.escalate.label"),
                    evidence=fill("signal.escalate.evidence", ref=r.number, days=days),
                    suggestedAction="Escalate to the section head to close the review.",
                )
            )
    return out
