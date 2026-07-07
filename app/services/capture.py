"""Guided Field Capture — service helpers.

Pure/near-pure functions kept out of the router so the idempotency and
triage-mapping logic is unit-testable without a DB (see tests/test_capture.py).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.capture import CaptureAttachment, CaptureSubmission, CaptureTaxonomy, TaxonomyAlias

TENANT_DEFAULT = "default"

# ── Anonymity ─────────────────────────────────────────────────────────────────
def anon_hash(user_id: str) -> str:
    """Stable HMAC of the reporter id — dedup/abuse control for anonymous
    reports without storing identity in the clear. Keyed off JWT_SECRET so
    the hash is deployment-specific and not rainbow-table-able."""
    key = ("capture-anon:" + (get_settings().jwt_secret or "")).encode()
    return hmac.new(key, user_id.encode(), hashlib.sha256).hexdigest()


def is_owner(sub: CaptureSubmission, user_id: str) -> bool:
    """The reporter always sees their own report — even an anonymous one
    (anonymity is against other viewers, not against yourself)."""
    if sub.reporterId and sub.reporterId == user_id:
        return True
    if sub.isAnonymous and sub.anonHash and sub.anonHash == anon_hash(user_id):
        return True
    return False


# ── Numbering ─────────────────────────────────────────────────────────────────
async def next_fld_number(db: AsyncSession, plant_code: str, plant_id: str) -> str:
    year = datetime.now(timezone.utc).year
    count = (
        await db.execute(
            select(func.count()).select_from(CaptureSubmission).where(CaptureSubmission.plantId == plant_id)
        )
    ).scalar_one()
    return f"FLD-{year}-{plant_code}-{count + 1:04d}"


# ── Triage: 5x5 mapping (technician never sees the matrix) ───────────────────
def risk_level_for(score: int) -> str:
    """Standard 5x5 banding — matches the platform's STD_5X5 defaults."""
    if score >= 17:
        return "CRITICAL"
    if score >= 10:
        return "HIGH"
    if score >= 5:
        return "MODERATE"
    return "LOW"


# self-reported severity → module Severity enum value (used until triage
# refines it, and for conversion when the officer skipped the matrix)
SELF_SEVERITY_TO_MODULE = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
RISK_LEVEL_TO_MODULE = {"LOW": "LOW", "MODERATE": "MEDIUM", "HIGH": "HIGH", "CRITICAL": "CRITICAL"}

# hazard taxonomy L1 code → ObservationCategory enum value (conversion map;
# OTHERS is the deliberate fallback — deeper classification is the officer's
# job, per spec 1.2 screen 2)
HAZARD_TO_OBS_CATEGORY = {
    "slip_trip_fall": "HOUSEKEEPING",
    "fire": "HOT_WORK",
    "electrical": "ELECTRICAL",
    "chemical": "CHEMICAL_HANDLING",
    "machine_guarding": "OTHERS",
    "housekeeping": "HOUSEKEEPING",
    "ppe": "PPE",
    "ergonomics": "OTHERS",
    "vehicle_forklift": "MOBILE_EQUIPMENT",
    "working_at_height": "WORK_AT_HEIGHT",
    "confined_space": "CONFINED_SPACE",
    "material_handling": "MATERIAL_HANDLING",
}


def synth_description(sub: CaptureSubmission) -> str:
    """Build a >=10-char narrative for module conversion when the technician
    (by design) typed nothing: category labels + transcript, English first."""
    parts: list[str] = []
    snap = sub.categorySnapshot or {}
    l1 = ((snap.get("l1") or {}).get("labels") or {}).get("en")
    l2 = ((snap.get("l2") or {}).get("labels") or {}).get("en")
    if l1:
        parts.append(f"{l1}{' — ' + l2 if l2 else ''}")
    if sub.description:
        parts.append(sub.description)
    text = sub.transcriptEnglish or sub.transcriptOriginal
    if text:
        parts.append(f'Reporter said: "{text}"')
    parts.append(f"Reported via guided field capture ({sub.number}); see attached media.")
    return " ".join(parts)


# ── Idempotency ───────────────────────────────────────────────────────────────
async def find_existing(db: AsyncSession, client_submission_id: str, tenant_id: str = TENANT_DEFAULT):
    return (
        await db.execute(
            select(CaptureSubmission)
            .where(CaptureSubmission.tenantId == tenant_id)
            .where(CaptureSubmission.clientSubmissionId == client_submission_id)
        )
    ).scalar_one_or_none()


# ── Taxonomy ──────────────────────────────────────────────────────────────────
async def taxonomy_version(db: AsyncSession) -> int:
    """Monotonic cache version = latest updatedAt epoch-seconds across the
    whole taxonomy. Backs the GET /taxonomy 304 contract."""
    latest = (await db.execute(select(func.max(CaptureTaxonomy.updatedAt)))).scalar_one_or_none()
    if latest is None:
        return 0
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return int(latest.timestamp())


async def resolve_code(db: AsyncSession, kind: str, code: str) -> CaptureTaxonomy | None:
    """Resolve a stable code to its node, following one alias hop for
    submissions synced with a stale offline taxonomy cache."""
    node = (
        await db.execute(
            select(CaptureTaxonomy).where(CaptureTaxonomy.kind == kind).where(CaptureTaxonomy.code == code)
        )
    ).scalar_one_or_none()
    if node is not None and node.active:
        return node
    alias = (
        await db.execute(
            select(TaxonomyAlias).where(TaxonomyAlias.kind == kind).where(TaxonomyAlias.fromCode == code)
        )
    ).scalar_one_or_none()
    if alias is None:
        return node
    return (
        await db.execute(
            select(CaptureTaxonomy).where(CaptureTaxonomy.kind == kind).where(CaptureTaxonomy.code == alias.toCode)
        )
    ).scalar_one_or_none()


def taxonomy_out(n: CaptureTaxonomy) -> dict[str, Any]:
    return {
        "id": n.id,
        "kind": n.kind,
        "level": n.level,
        "parentId": n.parentId,
        "code": n.code,
        "labels": n.labels or {},
        "iconKey": n.iconKey,
        "fishboneCategory": n.fishboneCategory,
        "sortWeight": n.sortWeight,
    }


# ── Serialisation ─────────────────────────────────────────────────────────────
def attachment_out(a: CaptureAttachment) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "fileName": a.fileName,
        "fileSize": a.fileSize,
        "mimeType": a.mimeType,
        "durationSec": a.durationSec,
        "caption": a.caption,
        "clientMediaId": a.clientMediaId,
        "uploadedAt": a.uploadedAt.isoformat() if a.uploadedAt else None,
    }


def submission_out(
    sub: CaptureSubmission,
    *,
    viewer_is_owner: bool = False,
    unmasked: bool = False,
    attachments: list[CaptureAttachment] | None = None,
) -> dict[str, Any]:
    """Anonymous submissions never expose reporter identity to non-owners
    unless explicitly unmasked (CAPTURE.UNMASK — audited)."""
    show_reporter = (not sub.isAnonymous) or viewer_is_owner or unmasked
    reporter = None
    if show_reporter and sub.reporter is not None:
        reporter = {"id": sub.reporter.id, "name": sub.reporter.name, "designation": sub.reporter.designation}
    return {
        "id": sub.id,
        "number": sub.number,
        "clientSubmissionId": sub.clientSubmissionId,
        "type": sub.type,
        "status": sub.status,
        "isAnonymous": sub.isAnonymous,
        "reporter": reporter,
        "plantId": sub.plantId,
        "areaId": sub.areaId,
        "mapPinX": sub.mapPinX,
        "mapPinY": sub.mapPinY,
        "equipmentId": sub.equipmentId,
        "qrScanned": sub.qrScanned,
        "categoryL1Id": sub.categoryL1Id,
        "categoryL2Id": sub.categoryL2Id,
        "categorySnapshot": sub.categorySnapshot,
        "aiSuggested": sub.aiSuggested,
        "aiConfidence": sub.aiConfidence,
        "severitySelfReported": sub.severitySelfReported,
        "description": sub.description,
        "voiceLangCode": sub.voiceLangCode,
        "transcriptOriginal": sub.transcriptOriginal,
        "transcriptEnglish": sub.transcriptEnglish,
        "transcriptionStatus": sub.transcriptionStatus,
        "triage": {
            "triagedById": sub.triagedById,
            "triagedAt": sub.triagedAt.isoformat() if sub.triagedAt else None,
            "hiraLikelihood": sub.hiraLikelihood,
            "hiraSeverity": sub.hiraSeverity,
            "riskScore": sub.riskScore,
            "riskLevel": sub.riskLevel,
            "note": sub.triageNote,
        },
        "converted": {
            "entityType": sub.convertedEntityType,
            "entityId": sub.convertedEntityId,
            "at": sub.convertedAt.isoformat() if sub.convertedAt else None,
        },
        "goldenThread": {
            "linkedRcaIds": sub.linkedRcaIds or [],
            "linkedCapaIds": sub.linkedCapaIds or [],
            "linkedPtwIds": sub.linkedPtwIds or [],
        },
        "capture": {
            "tapCount": sub.tapCount,
            "durationMs": sub.durationMs,
            "offline": sub.wasOffline,
            "appVersion": sub.appVersion,
            "deviceLang": sub.deviceLang,
        },
        "createdAtClient": sub.createdAtClient.isoformat() if sub.createdAtClient else None,
        "createdAt": sub.createdAt.isoformat() if sub.createdAt else None,
        "attachments": [attachment_out(a) for a in (attachments or [])],
    }


__all__ = [
    "TENANT_DEFAULT",
    "anon_hash",
    "is_owner",
    "next_fld_number",
    "risk_level_for",
    "SELF_SEVERITY_TO_MODULE",
    "RISK_LEVEL_TO_MODULE",
    "HAZARD_TO_OBS_CATEGORY",
    "synth_description",
    "find_existing",
    "taxonomy_version",
    "resolve_code",
    "taxonomy_out",
    "attachment_out",
    "submission_out",
]
