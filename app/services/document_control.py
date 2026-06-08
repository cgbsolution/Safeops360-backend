"""Document Control service (Pharma IMS Module 2).

Controlled-document lifecycle: draft → technical review (e-sign) → QA review
(e-sign) → approval (e-sign) → effective (supersedes the prior version) →
distribution + read-acknowledgment (e-sign). Each review/approval/ack is a
21 CFR Part 11 electronic signature over the version; every transition writes a
GMP audit entry. Service functions flush; the router commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_control import ControlledDocument, DocumentVersion
from app.models.plant import Plant
from app.models.user import User
from app.services import part11

TYPE_PREFIX = {
    "sop": "SOP", "work_instruction": "WI", "specification": "SPEC",
    "batch_manufacturing_record": "BMR", "batch_packaging_record": "BPR",
    "form": "FORM", "validation_protocol": "VAL", "validation_report": "VAL",
    "method": "MTH", "cleaning_procedure": "CLN", "policy": "POL",
    "training_material_gmp": "TRN", "other": "DOC",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def version_snapshot(v: DocumentVersion, doc: ControlledDocument) -> dict[str, Any]:
    """Substantive content a version signature binds to (excludes volatile
    status / dates)."""
    return {
        "documentNumber": doc.documentNumber,
        "title": doc.title,
        "version": v.version,
        "changeSummary": v.changeSummary,
        "documentFileHash": v.documentFileHash,
    }


async def _plant_code(db: AsyncSession, plant_id: str) -> str:
    p = await db.get(Plant, plant_id)
    return (p.code if p and p.code else plant_id[:4]).upper()


async def _next_number(db: AsyncSession, document_type: str) -> str:
    prefix = TYPE_PREFIX.get(document_type, "DOC")
    n = (await db.execute(select(func.count(ControlledDocument.id)).where(ControlledDocument.documentNumber.like(f"{prefix}-%")))).scalar_one()
    return f"{prefix}-{(n + 1):04d}"


def _bump_major(version: str) -> str:
    try:
        major = int(str(version).split(".")[0])
    except ValueError:
        major = 1
    return f"{major + 1}.0"


# ─── Create + revise ─────────────────────────────────────────────────────


async def create_document(db: AsyncSession, *, user: User, data: dict[str, Any]) -> ControlledDocument:
    number = data.get("documentNumber") or await _next_number(db, data["documentType"])
    now = _utcnow()
    doc = ControlledDocument(
        tenantId=None, documentNumber=number, title=data["title"], documentType=data["documentType"],
        category=data.get("category", ""), plantId=data["plantId"],
        currentVersion="1.0", currentVersionStatus="draft",
        reviewFrequencyMonths=data.get("reviewFrequencyMonths", 24),
        reviewOwnerUserId=data.get("reviewOwnerUserId"),
        applicableAreas=data.get("applicableAreas", []), applicableRoles=data.get("applicableRoles", []),
        applicableProducts=data.get("applicableProducts", []),
        requiresTrainingOnNewVersion=data.get("requiresTrainingOnNewVersion", False),
        distributionList=[{"userId": u, "role": "", "distribution_status": "not_sent",
                           "sent_at": None, "acknowledged_at": None, "acknowledgment_signature_id": None}
                          for u in data.get("distributeToUserIds", [])],
        referencedDocuments=data.get("referencedDocuments", []),
        regulatoryReference=data.get("regulatoryReference", ""),
        retentionYears=data.get("retentionYears", 7),
        createdByUserId=user.id,
    )
    db.add(doc)
    await db.flush()
    v = DocumentVersion(
        documentId=doc.id, version="1.0", status="draft", authoredByUserId=user.id, authoredAt=now,
        changeSummary=data.get("changeSummary", "Initial issue."), documentFileUrl=data.get("documentFileUrl"),
        documentFileHash=data.get("documentFileHash"),
    )
    db.add(v)
    await db.flush()
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="created", user=user, new_value=f"{doc.documentNumber} v1.0", reason="Document created")
    return doc


async def revise(db: AsyncSession, *, doc: ControlledDocument, user: User, change_summary: str,
                 file_url: str | None = None, file_hash: str | None = None) -> DocumentVersion:
    new_version = _bump_major(doc.currentVersion)
    v = DocumentVersion(
        documentId=doc.id, version=new_version, status="draft", authoredByUserId=user.id, authoredAt=_utcnow(),
        changeSummary=change_summary, documentFileUrl=file_url, documentFileHash=file_hash,
    )
    db.add(v)
    await db.flush()
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="modified", user=user, field_name="version", old_value=doc.currentVersion,
                             new_value=new_version, reason=f"Revision started: {change_summary}")
    return v


# ─── Lifecycle transitions ───────────────────────────────────────────────


async def _latest_draft(db: AsyncSession, doc_id: str) -> DocumentVersion | None:
    return (await db.execute(
        select(DocumentVersion).where(DocumentVersion.documentId == doc_id)
        .where(DocumentVersion.status.in_(["draft", "under_review", "approved"]))
        .order_by(DocumentVersion.createdAt.desc()).limit(1)
    )).scalar_one_or_none()


async def submit_for_review(db: AsyncSession, *, doc: ControlledDocument, user: User) -> DocumentVersion:
    await db.flush()  # ensure pending status changes are visible to the query below
    v = await _latest_draft(db, doc.id)
    if v is None or v.status != "draft":
        raise ValueError("No draft version to submit.")
    v.status = "under_review"
    doc.currentVersionStatus = "under_review"
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="status_changed", user=user, field_name="status",
                             old_value="draft", new_value="under_review", reason="Submitted for review")
    return v


async def _sign_step(db: AsyncSession, *, doc: ControlledDocument, user: User, password: str, ip: str | None,
                     meaning: str) -> DocumentVersion:
    await db.flush()
    v = await _latest_draft(db, doc.id)
    if v is None:
        raise ValueError("No in-review version.")
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — signature not applied.")
    await part11.sign(db, user=user, record_type="document_version", record_id=v.id, record_number=f"{doc.documentNumber} {v.version}",
                      meaning=meaning, record_snapshot=version_snapshot(v, doc), ip=ip)
    return v


async def technical_review(db: AsyncSession, *, doc: ControlledDocument, user: User, password: str, ip: str | None = None) -> DocumentVersion:
    v = await _sign_step(db, doc=doc, user=user, password=password, ip=ip, meaning="Reviewed — Technical Reviewer")
    v.technicalReviewByUserId = user.id
    v.technicalReviewAt = _utcnow()
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="modified", user=user, field_name="technicalReview", new_value="signed", reason="Technical review")
    return v


async def qa_review(db: AsyncSession, *, doc: ControlledDocument, user: User, password: str, ip: str | None = None) -> DocumentVersion:
    v = await _sign_step(db, doc=doc, user=user, password=password, ip=ip, meaning="Reviewed — QA Reviewer")
    v.qaReviewByUserId = user.id
    v.qaReviewAt = _utcnow()
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="modified", user=user, field_name="qaReview", new_value="signed", reason="QA review")
    return v


async def approve(db: AsyncSession, *, doc: ControlledDocument, user: User, password: str,
                  effective_from: datetime | None = None, ip: str | None = None) -> DocumentVersion:
    v = await _sign_step(db, doc=doc, user=user, password=password, ip=ip, meaning="Reviewed and Approved")
    now = _utcnow()
    v.approvedByUserId = user.id
    v.approvedAt = now
    v.status = "approved"
    v.effectiveFrom = effective_from or now
    doc.currentVersionStatus = "approved"
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="status_changed", user=user, field_name="status",
                             old_value="under_review", new_value="approved", reason="Document approved")
    return v


async def make_effective(db: AsyncSession, *, doc: ControlledDocument, user: User) -> DocumentVersion:
    await db.flush()
    v = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.documentId == doc.id).where(DocumentVersion.status == "approved")
        .order_by(DocumentVersion.createdAt.desc()).limit(1)
    )).scalar_one_or_none()
    if v is None:
        raise ValueError("No approved version to make effective.")
    now = _utcnow()
    # Supersede the prior effective version.
    prior = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.documentId == doc.id).where(DocumentVersion.status == "effective")
    )).scalars().all()
    for p in prior:
        p.status = "superseded"
        p.supersededAt = now
    v.status = "effective"
    v.effectiveFrom = v.effectiveFrom or now
    doc.currentVersion = v.version
    doc.currentVersionStatus = "effective"
    doc.currentVersionEffectiveFrom = v.effectiveFrom
    doc.currentDocumentFileUrl = v.documentFileUrl
    doc.currentDocumentFileHash = v.documentFileHash
    doc.nextReviewDue = now + timedelta(days=30 * doc.reviewFrequencyMonths)
    # (Re)issue distribution.
    dl = list(doc.distributionList or [])
    for entry in dl:
        entry["distribution_status"] = "sent"
        entry["sent_at"] = now.isoformat()
        entry["acknowledged_at"] = None
        entry["acknowledgment_signature_id"] = None
    doc.distributionList = dl
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="status_changed", user=user, field_name="status",
                             old_value="approved", new_value="effective",
                             reason=f"v{v.version} effective; prior superseded; distribution issued")
    return v


async def acknowledge(db: AsyncSession, *, doc: ControlledDocument, user: User, password: str, ip: str | None = None) -> ControlledDocument:
    if not part11.check_password(user, password):
        raise part11.SignatureError("Password verification failed — acknowledgment not recorded.")
    v = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.documentId == doc.id).where(DocumentVersion.status == "effective").limit(1)
    )).scalar_one_or_none()
    sig = await part11.sign(db, user=user, record_type="document_ack", record_id=doc.id, record_number=doc.documentNumber,
                            meaning="Read and Understood", record_snapshot=version_snapshot(v, doc) if v else {"documentNumber": doc.documentNumber},
                            ip=ip)
    await db.flush()
    now = _utcnow()
    dl = list(doc.distributionList or [])
    found = False
    for entry in dl:
        if entry.get("userId") == user.id:
            entry["distribution_status"] = "acknowledged"
            entry["acknowledged_at"] = now.isoformat()
            entry["acknowledgment_signature_id"] = sig.id
            entry["role"] = user.role
            found = True
    if not found:
        dl.append({"userId": user.id, "role": user.role, "distribution_status": "acknowledged",
                   "sent_at": now.isoformat(), "acknowledged_at": now.isoformat(), "acknowledgment_signature_id": sig.id})
    doc.distributionList = dl
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="signed", user=user, field_name="acknowledgment", new_value="Read and Understood", reason="Document acknowledged")
    return doc


async def obsolete(db: AsyncSession, *, doc: ControlledDocument, user: User, reason: str) -> ControlledDocument:
    doc.currentVersionStatus = "obsolete"
    for v in (await db.execute(select(DocumentVersion).where(DocumentVersion.documentId == doc.id).where(DocumentVersion.status == "effective"))).scalars().all():
        v.status = "obsolete"
    await part11.write_audit(db, record_type="document", record_id=doc.id, record_number=doc.documentNumber,
                             event_type="status_changed", user=user, field_name="status", new_value="obsolete", reason=reason or "Obsoleted")
    return doc


# ─── Serialisation ───────────────────────────────────────────────────────


def to_dict(doc: ControlledDocument) -> dict[str, Any]:
    now = _utcnow()
    dl = doc.distributionList or []
    acked = sum(1 for e in dl if e.get("distribution_status") == "acknowledged")
    due = part11_aware(doc.nextReviewDue)
    return {
        "id": doc.id, "documentNumber": doc.documentNumber, "title": doc.title,
        "documentType": doc.documentType, "category": doc.category, "plantId": doc.plantId,
        "currentVersion": doc.currentVersion, "currentVersionStatus": doc.currentVersionStatus,
        "currentVersionEffectiveFrom": _iso(doc.currentVersionEffectiveFrom),
        "nextReviewDue": _iso(doc.nextReviewDue),
        "reviewOverdue": bool(due and now > due),
        "reviewFrequencyMonths": doc.reviewFrequencyMonths,
        "applicableAreas": doc.applicableAreas or [], "applicableProducts": doc.applicableProducts or [],
        "distributionTotal": len(dl), "distributionAcked": acked,
        "regulatoryReference": doc.regulatoryReference, "retentionYears": doc.retentionYears,
        "requiresTrainingOnNewVersion": doc.requiresTrainingOnNewVersion,
        "createdAt": _iso(doc.createdAt),
    }


def part11_aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def version_dict(v: DocumentVersion) -> dict[str, Any]:
    return {
        "id": v.id, "version": v.version, "status": v.status, "changeSummary": v.changeSummary,
        "authoredByUserId": v.authoredByUserId, "authoredAt": _iso(v.authoredAt),
        "technicalReviewAt": _iso(v.technicalReviewAt), "qaReviewAt": _iso(v.qaReviewAt),
        "approvedAt": _iso(v.approvedAt), "effectiveFrom": _iso(v.effectiveFrom), "supersededAt": _iso(v.supersededAt),
        "documentFileHash": v.documentFileHash,
    }


async def dashboard(db: AsyncSession, *, plant_id: str) -> dict[str, Any]:
    docs = (await db.execute(select(ControlledDocument).where(ControlledDocument.plantId == plant_id))).scalars().all()
    now = _utcnow()
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    review_overdue = effective = ack_pending = 0
    for d in docs:
        by_status[d.currentVersionStatus] = by_status.get(d.currentVersionStatus, 0) + 1
        by_type[d.documentType] = by_type.get(d.documentType, 0) + 1
        due = part11_aware(d.nextReviewDue)
        if due and now > due:
            review_overdue += 1
        if d.currentVersionStatus == "effective":
            effective += 1
        dl = d.distributionList or []
        if any(e.get("distribution_status") == "sent" for e in dl):
            ack_pending += 1
    return {
        "plantId": plant_id, "total": len(docs), "effective": effective,
        "reviewOverdue": review_overdue, "ackPending": ack_pending,
        "byStatus": by_status, "byType": by_type,
    }
