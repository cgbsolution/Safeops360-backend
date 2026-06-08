"""21 CFR Part 11 / EU Annex 11 service — electronic signatures + GMP audit.

Two primitives, reusable by any GMP-regulated module:

  verify_and_sign()  — re-authenticate the signer (password re-entry, verified
                       server-side against the bcrypt hash) and write an
                       ElectronicSignature that snapshots the signer's identity
                       and a hash of the record AT signing time. Any later edit
                       to the record changes its hash, so is_valid() flips false
                       — the signature can be neither excised nor transferred.

  write_audit()      — write a computer-generated, time-stamped GmpAuditEntry
                       with old→new value + reason-for-change. The entry carries
                       a tamper-evident hash; recompute it to prove the row was
                       not altered. Append-only — there is no update path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.models.deviation import Deviation
from app.models.part11 import ElectronicSignature, GmpAuditEntry
from app.models.user import User


class SignatureError(Exception):
    """Raised when re-authentication fails — the signature is NOT applied."""


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canonical(d: dict[str, Any]) -> str:
    return json.dumps(d, sort_keys=True, default=str)


def _ts_ms(dt: datetime) -> str:
    """Millisecond epoch — deterministic across a Postgres round-trip, so audit
    / signature hashes recompute identically at verification time."""
    return str(int(dt.timestamp() * 1000))


# ─── Record snapshots (what a signature/hash is taken over) ──────────────


def deviation_snapshot(d: Deviation) -> dict[str, Any]:
    """The SUBSTANTIVE content a signature binds to — editing any of these after
    signing invalidates the signature (tamper-evidence). Deliberately excludes
    the workflow `status` (and capaNumber): those legitimately advance after a
    disposition is signed, and a valid signature must survive that progression
    while still breaking on any edit to the reviewed content."""
    return {
        "number": d.number,
        "title": d.title,
        "description": d.description,
        "type": d.type,
        "category": d.category,
        "severity": d.severity,
        "rootCauseCategory": d.rootCauseCategory,
        "rootCauseDescription": d.rootCauseDescription,
        "batchDispositionRecommendation": d.batchDispositionRecommendation,
        "batchDispositionJustification": d.batchDispositionJustification,
        "impactAssessment": d.impactAssessment,
    }


async def snapshot_for(db: AsyncSession, record_type: str, record_id: str) -> dict[str, Any]:
    """Server-built snapshot for a known record type (so the hash covers real
    content, not a client-supplied blob)."""
    if record_type == "deviation":
        d = await db.get(Deviation, record_id)
        if d is not None:
            return deviation_snapshot(d)
    return {"recordType": record_type, "recordId": record_id}


# ─── Audit trail ─────────────────────────────────────────────────────────


async def write_audit(
    db: AsyncSession,
    *,
    record_type: str,
    record_id: str,
    record_number: str | None,
    event_type: str,
    user: User,
    field_name: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
    reason: str = "",
    ip: str | None = None,
    user_agent: str | None = None,
    session_id: str | None = None,
) -> GmpAuditEntry:
    now = datetime.now(timezone.utc)
    old_s = None if old_value is None else str(old_value)
    new_s = None if new_value is None else str(new_value)
    payload = "|".join([
        record_type, record_id, event_type, _ts_ms(now), user.id,
        field_name or "", old_s or "", new_s or "", reason or "",
    ])
    entry = GmpAuditEntry(
        tenantId=None,
        recordType=record_type,
        recordId=record_id,
        recordNumber=record_number,
        eventType=event_type,
        eventAt=now,
        eventByUserId=user.id,
        eventByFullName=user.name,
        eventByRole=user.role,
        fieldName=field_name,
        oldValue=old_s,
        newValue=new_s,
        reasonForChange=reason,
        ipAddress=ip,
        userAgent=user_agent,
        sessionId=session_id,
        entryHash=_sha256(payload),
    )
    db.add(entry)
    return entry


def audit_entry_is_intact(entry: GmpAuditEntry) -> bool:
    payload = "|".join([
        entry.recordType, entry.recordId, entry.eventType, _ts_ms(entry.eventAt),
        entry.eventByUserId, entry.fieldName or "", entry.oldValue or "",
        entry.newValue or "", entry.reasonForChange or "",
    ])
    return entry.entryHash == _sha256(payload)


# ─── Electronic signatures ───────────────────────────────────────────────


def check_password(user: User, password: str) -> bool:
    """Re-authentication gate (21 CFR 11.200) — verify the signer's password
    server-side against the bcrypt hash before any signed action proceeds."""
    return verify_password(password, user.passwordHash)


async def sign(
    db: AsyncSession,
    *,
    user: User,
    record_type: str,
    record_id: str,
    record_number: str | None,
    meaning: str,
    record_snapshot: dict[str, Any],
    ip: str | None = None,
) -> ElectronicSignature:
    """Write a Part 11 electronic signature over `record_snapshot`. The caller
    MUST have already verified the password (check_password) and applied the
    change being signed, so the signature binds to the final reviewed content."""
    now = datetime.now(timezone.utc)
    record_hash = _sha256(_canonical(record_snapshot))
    sig_payload = "|".join([record_type, record_id, user.id, meaning, _ts_ms(now), record_hash])
    sig = ElectronicSignature(
        tenantId=None,
        recordType=record_type,
        recordId=record_id,
        recordNumber=record_number,
        signerUserId=user.id,
        signerFullName=user.name,
        signerRole=user.role,
        signerDepartment=user.department,
        signedAt=now,
        signatureMeaning=meaning,
        ipAddress=ip,
        reAuthenticated=True,
        authenticationMethod="password",
        recordHash=record_hash,
        signatureHash=_sha256(sig_payload),
        isValid=True,
    )
    db.add(sig)
    # The act of signing is itself an audited event.
    await write_audit(
        db, record_type=record_type, record_id=record_id, record_number=record_number,
        event_type="signed", user=user, field_name="electronic_signature",
        new_value=meaning, reason=meaning, ip=ip,
    )
    return sig


async def verify_and_sign(
    db: AsyncSession,
    *,
    user: User,
    record_type: str,
    record_id: str,
    record_number: str | None,
    meaning: str,
    password: str,
    record_snapshot: dict[str, Any],
    ip: str | None = None,
) -> ElectronicSignature:
    """Re-authenticate then sign over the CURRENT snapshot (used by the generic
    /api/esign endpoint). Raises SignatureError if the password is wrong."""
    if not check_password(user, password):
        raise SignatureError("Password verification failed — signature not applied.")
    return await sign(
        db, user=user, record_type=record_type, record_id=record_id,
        record_number=record_number, meaning=meaning, record_snapshot=record_snapshot, ip=ip,
    )


def signature_is_valid(sig: ElectronicSignature, current_snapshot: dict[str, Any]) -> bool:
    """A signature is valid iff the record is unchanged since it was signed."""
    return sig.recordHash == _sha256(_canonical(current_snapshot))


async def signatures_for(
    db: AsyncSession, record_type: str, record_id: str, current_snapshot: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ElectronicSignature)
            .where(ElectronicSignature.recordType == record_type)
            .where(ElectronicSignature.recordId == record_id)
            .order_by(ElectronicSignature.signedAt.asc())
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for s in rows:
        valid = signature_is_valid(s, current_snapshot) if current_snapshot is not None else s.isValid
        out.append({
            "id": s.id,
            "signerFullName": s.signerFullName,
            "signerRole": s.signerRole,
            "signerDepartment": s.signerDepartment,
            "signatureMeaning": s.signatureMeaning,
            "signedAt": s.signedAt.isoformat() if s.signedAt else None,
            "reAuthenticated": s.reAuthenticated,
            "authenticationMethod": s.authenticationMethod,
            "isValid": valid,
        })
    return out


async def audit_for(db: AsyncSession, record_type: str, record_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(GmpAuditEntry)
            .where(GmpAuditEntry.recordType == record_type)
            .where(GmpAuditEntry.recordId == record_id)
            .order_by(GmpAuditEntry.eventAt.asc())
        )
    ).scalars().all()
    return [
        {
            "id": e.id,
            "eventType": e.eventType,
            "eventAt": e.eventAt.isoformat() if e.eventAt else None,
            "eventByFullName": e.eventByFullName,
            "eventByRole": e.eventByRole,
            "fieldName": e.fieldName,
            "oldValue": e.oldValue,
            "newValue": e.newValue,
            "reasonForChange": e.reasonForChange,
            "intact": audit_entry_is_intact(e),
        }
        for e in rows
    ]
