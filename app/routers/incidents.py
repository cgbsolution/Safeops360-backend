from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.incident import (
    Incident,
    IncidentAttachment,
    IncidentInvestigationMember,
    IncidentStatus,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.incident import (
    AttachmentInit,
    AttachmentOut,
    IncidentCreate,
    IncidentOut,
    IncidentUpdate,
)
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)
from app.services.rca import generate_rca_summary, normalise_rca_method
from app.services.storage import (
    build_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

VALID_CATEGORIES = {
    "INITIAL_PHOTO", "WITNESS_STATEMENT", "CCTV", "EQUIPMENT_DATA",
    "DOCUMENT", "SKETCH", "EXTERNAL_REPORT", "CAPA_EVIDENCE", "CLOSURE_DOC",
}
ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/quicktime",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "text/plain",
}
MAX_FILE_SIZE = 50 * 1024 * 1024


@router.get("")
async def list_incidents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "INCIDENT.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Incident)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(Incident.plantId.in_(plants))
    if read_check.matched_scope == "OWN_RECORDS":
        stmt = stmt.where(Incident.reporterId == user.id)
    rows = (await db.execute(stmt.order_by(Incident.date.desc()).limit(100))).scalars().all()
    return {"items": [IncidentOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
async def create_incident(
    payload: IncidentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    await require_permission_with_context("INCIDENT.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")
    if payload.date.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incident date cannot be in the future.")

    rca_method = normalise_rca_method(payload.rootCauseMethod)
    rca_data = payload.rootCauseData
    rca_summary = generate_rca_summary(rca_method, rca_data) if rca_method else None

    last = (
        await db.execute(select(func.count()).select_from(Incident).where(Incident.plantId == payload.plantId))
    ).scalar_one()
    number = f"INC-{payload.date.year}-{plant.code}-{last + 1:04d}"

    incident = Incident(
        number=number,
        date=payload.date,
        type=payload.type,
        plantId=payload.plantId,
        areaId=payload.areaId,
        location=payload.location,
        reporterId=user.id,
        injuredPersonName=payload.injuredPersonName,
        injuredPersonAge=payload.injuredPersonAge,
        injuredPersonDesignation=payload.injuredPersonDesignation,
        bodyPart=payload.bodyPart,
        natureOfInjury=payload.natureOfInjury,
        description=payload.description,
        immediateCause=payload.immediateCause,
        rootCauseMethod=rca_method,
        rootCauseDetail=None if rca_data else payload.rootCauseDetail,
        rootCauseData=rca_data,
        rootCauseSummary=rca_summary,
        correctiveActions=payload.correctiveActions,
        preventiveActions=payload.preventiveActions,
        lostDays=max(0, payload.lostDays or 0),
        propertyDamageCost=payload.propertyDamageCost,
        status=IncidentStatus.REPORTED,
    )
    db.add(incident)
    await db.flush()

    if payload.investigationTeamIds:
        for i, uid in enumerate(payload.investigationTeamIds):
            db.add(IncidentInvestigationMember(incidentId=incident.id, userId=uid, role="LEAD" if i == 0 else "MEMBER"))

    try:
        await workflow_engine.initiate(
            db,
            module="INCIDENT",
            record_id=incident.id,
            record_number=incident.number,
            record_title=incident.description[:120],
            record_data={
                "type": incident.type.value,
                "plantId": incident.plantId,
                "reporterId": incident.reporterId,
                "lostDays": incident.lostDays,
            },
            initiator_id=user.id,
            plant_id=incident.plantId,
        )
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"Incident workflow init failed: {e}", file=sys.stderr)

    return IncidentOut.model_validate(incident)


@router.get("/{incident_id}", response_model=IncidentOut)
async def get_incident(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return IncidentOut.model_validate(incident)


@router.patch("/{incident_id}", response_model=IncidentOut)
async def update_incident(
    incident_id: str,
    payload: IncidentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if incident.status == IncidentStatus.CLOSED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot edit a closed incident.")

    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if payload.immediateCause is not None:
        incident.immediateCause = payload.immediateCause or None
    if payload.rootCauseMethod is not None:
        incident.rootCauseMethod = normalise_rca_method(payload.rootCauseMethod)
    if payload.rootCauseDetail is not None:
        incident.rootCauseDetail = payload.rootCauseDetail or None
    if payload.rootCauseData is not None:
        incident.rootCauseData = payload.rootCauseData or None
        method = normalise_rca_method(incident.rootCauseMethod)
        incident.rootCauseSummary = generate_rca_summary(method, payload.rootCauseData) if method else None
    if payload.correctiveActions is not None:
        incident.correctiveActions = payload.correctiveActions or None
    if payload.preventiveActions is not None:
        incident.preventiveActions = payload.preventiveActions or None
    if payload.lostDays is not None:
        if payload.lostDays < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Lost days must be ≥ 0.")
        incident.lostDays = payload.lostDays
    if payload.propertyDamageCost is not None:
        if payload.propertyDamageCost < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Property damage cost must be ≥ 0.")
        incident.propertyDamageCost = payload.propertyDamageCost

    if payload.investigationTeamIds is not None:
        # Replace team
        existing = (
            await db.execute(select(IncidentInvestigationMember).where(IncidentInvestigationMember.incidentId == incident.id))
        ).scalars().all()
        for m in existing:
            await db.delete(m)
        for i, uid in enumerate(payload.investigationTeamIds):
            db.add(IncidentInvestigationMember(incidentId=incident.id, userId=uid, role="LEAD" if i == 0 else "MEMBER"))

    await db.flush()
    return IncidentOut.model_validate(incident)


# ─── Attachments ─────────────────────────────────────────────────────────


@router.get("/{incident_id}/attachments")
async def list_attachments(
    incident_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(IncidentAttachment)
            .options(selectinload(IncidentAttachment.uploadedBy))
            .where(IncidentAttachment.incidentId == incident_id)
            .where(IncidentAttachment.deletedAt.is_(None))
            .order_by(IncidentAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [AttachmentOut.model_validate(r) for r in rows]}


@router.post("/{incident_id}/attachments")
async def upload_attachment(
    incident_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")

    record = {"reporterId": incident.reporterId}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=incident.id, plant_id=incident.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        try:
            init = AttachmentInit(**payload)
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid init body: {e}") from e
        if init.category not in VALID_CATEGORIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}")
        if init.fileSize > MAX_FILE_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"File size exceeds the {MAX_FILE_SIZE // 1024 // 1024} MB limit.")
        if init.mimeType not in ALLOWED_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {init.mimeType} is not allowed.")
        storage_path = build_storage_path(incident_id=incident_id, category=init.category, file_name=init.fileName)
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Storage upload init failed: {e}",
            ) from e
        att = IncidentAttachment(
            incidentId=incident_id,
            category=init.category,
            fileName=init.fileName,
            storagePath=storage_path,
            fileSize=init.fileSize,
            mimeType=init.mimeType,
            uploadedById=user.id,
            capaRef=init.capaRef,
            witnessRef=init.witnessRef,
        )
        db.add(att)
        await db.flush()
        return {
            "phase": "init",
            "attachmentId": att.id,
            "storagePath": storage_path,
            "uploadUrl": signed["uploadUrl"],
            "token": signed["token"],
        }

    if phase == "complete":
        attachment_id = payload.get("attachmentId")
        if not attachment_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "attachmentId required")
        att = await db.get(IncidentAttachment, attachment_id)
        if att is None or att.incidentId != incident_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this incident")
        att.caption = payload.get("caption")
        att.exifData = payload.get("exifData")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.delete("/{incident_id}/attachments/{attachment_id}")
async def delete_attachment(
    incident_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(IncidentAttachment, attachment_id)
    if att is None or att.incidentId != incident_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    incident = await db.get(Incident, incident_id)
    record = {"reporterId": incident.reporterId if incident else None, "uploadedById": att.uploadedById}
    result = await can(
        db, user.id, "INCIDENT.UPDATE",
        PermissionContext(record_id=att.id, plant_id=incident.plantId if incident else None, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}


@router.get("/{incident_id}/attachments/{attachment_id}/download")
async def download_attachment(
    incident_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(IncidentAttachment, attachment_id)
    if att is None or att.incidentId != incident_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    incident = await db.get(Incident, incident_id)
    record = {"reporterId": incident.reporterId if incident else None}
    result = await can(
        db, user.id, "INCIDENT.READ",
        PermissionContext(record_id=incident.id if incident else None, plant_id=incident.plantId if incident else None, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}
