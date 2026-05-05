from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.flra import FLRA, FLRACrewSignature, FLRAStatus, FLRATeamMember
from app.models.permit import Permit, PermitStatus
from app.models.plant import Plant
from app.models.training import TrainingProgram, TrainingRecord
from app.models.user import User
from app.models.workflow import Action, WorkflowHistory, WorkflowInstance
from app.schemas.flra import FLRACreate, FLRAOut, FLRARedoRequest
from app.services.flra_gate import maybe_complete_flra, resolve_crew_for_flra
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_user_role_codes,
)
from app.routers.ptw import REQUIRED_TRAINING_CODES

router = APIRouter(prefix="/api/flra", tags=["flra"])


@router.get("")
async def list_flras(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "FLRA.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(FLRA)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(FLRA.plantId.in_(plants))
    rows = (await db.execute(stmt.order_by(FLRA.date.desc()).limit(100))).scalars().all()
    return {"items": [FLRAOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=FLRAOut, status_code=status.HTTP_201_CREATED)
async def create_flra(
    payload: FLRACreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FLRAOut:
    await require_permission_with_context("FLRA.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    # Linked-permit validation
    if payload.permitId:
        permit = await db.get(Permit, payload.permitId)
        if permit is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Linked permit not found.")
        if permit.status in {PermitStatus.DRAFT, PermitStatus.REJECTED, PermitStatus.EXPIRED, PermitStatus.CLOSED}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot start FLRA on a {permit.status.value} permit.")
        active_q = select(func.count()).select_from(FLRA).where(
            FLRA.permitId == payload.permitId,
            FLRA.status.in_([FLRAStatus.IN_PROGRESS, FLRAStatus.COMPLETED]),
        )
        if (await db.execute(active_q)).scalar_one():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Permit {permit.number} already has an active FLRA. Use Re-do FLRA instead.",
            )

    # Hazards must be a non-empty JSON array
    try:
        hz = json.loads(payload.hazards or "[]")
    except json.JSONDecodeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hazards data is malformed.") from e
    filled = [
        h for h in hz if isinstance(h, dict) and (str(h.get("step", "")).strip() or str(h.get("hazard", "")).strip())
    ]
    if not filled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Capture at least one hazard before saving.")

    last = (
        await db.execute(select(func.count()).select_from(FLRA).where(FLRA.plantId == payload.plantId))
    ).scalar_one()
    number = f"FLRA-{plant.code}-{last + 1:04d}"

    crew_ids = await resolve_crew_for_flra(
        db,
        permit_id=payload.permitId,
        fallback_team_member_ids=payload.teamMemberIds,
    )

    flra = FLRA(
        number=number,
        permitId=payload.permitId,
        plantId=payload.plantId,
        date=payload.date,
        location=payload.location,
        jobDescription=payload.jobDescription,
        leaderId=user.id,
        hazards=json.dumps(filled),
        toolboxTalkById=payload.toolboxTalkById,
        toolboxTalkConfirmed=payload.toolboxTalkConfirmed,
        status=FLRAStatus.IN_PROGRESS,
    )
    db.add(flra)
    await db.flush()
    for uid in payload.teamMemberIds:
        db.add(FLRATeamMember(flraId=flra.id, userId=uid))
    for uid in crew_ids:
        db.add(FLRACrewSignature(flraId=flra.id, userId=uid))
    await db.flush()
    return FLRAOut.model_validate(flra)


@router.get("/{flra_id}", response_model=FLRAOut)
async def get_flra(
    flra_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FLRAOut:
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    record = {"leaderId": flra.leaderId}
    result = await can(
        db, user.id, "FLRA.READ",
        PermissionContext(record_id=flra.id, plant_id=flra.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return FLRAOut.model_validate(flra)


@router.post("/{flra_id}/sign")
async def sign_flra(
    flra_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-crew sign-off with training re-validation. Mirror of Node /sign route."""
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "FLRA not found")
    if flra.status == FLRAStatus.SUPERSEDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This FLRA has been superseded by a re-do — sign the new one instead.")
    if flra.status == FLRAStatus.CANCELLED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This FLRA has been cancelled.")
    if flra.status == FLRAStatus.COMPLETED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This FLRA is already complete — no further signatures needed.")

    # Permit-state lock
    permit = await db.get(Permit, flra.permitId) if flra.permitId else None
    if permit and permit.status in {PermitStatus.SUSPENDED, PermitStatus.EXPIRED, PermitStatus.CLOSED, PermitStatus.REJECTED}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Linked permit is {permit.status.value}. FLRA actions are locked.")

    sig_q = select(FLRACrewSignature).where(
        FLRACrewSignature.flraId == flra.id, FLRACrewSignature.userId == user.id
    )
    sig = (await db.execute(sig_q)).scalar_one_or_none()
    if sig is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You are not listed on this FLRA's crew. Ask the crew leader to add you before signing.",
        )
    if sig.signed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You have already signed this FLRA.")

    # Training re-check
    training_valid = True
    training_expires_at: datetime | None = None
    if permit:
        required_code = REQUIRED_TRAINING_CODES.get(permit.type.value)
        if required_code:
            prog = (
                await db.execute(select(TrainingProgram).where(TrainingProgram.code == required_code))
            ).scalar_one_or_none()
            if prog is not None:
                now = datetime.now(timezone.utc)
                tr_stmt = (
                    select(TrainingRecord)
                    .where(TrainingRecord.employeeId == user.id)
                    .where(TrainingRecord.programId == prog.id)
                    .where(TrainingRecord.passed == True)
                    .where(TrainingRecord.validUntil > now)
                    .order_by(TrainingRecord.validUntil.desc())
                    .limit(1)
                )
                valid = (await db.execute(tr_stmt)).scalar_one_or_none()
                if valid is None:
                    last_q = (
                        select(TrainingRecord)
                        .where(TrainingRecord.employeeId == user.id)
                        .where(TrainingRecord.programId == prog.id)
                        .where(TrainingRecord.passed == True)
                        .order_by(TrainingRecord.validUntil.desc())
                        .limit(1)
                    )
                    last_record = (await db.execute(last_q)).scalar_one_or_none()
                    expiry = last_record.validUntil.strftime("%d %b %Y") if last_record else "never"
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f'Your "{prog.name}" training expired on {expiry}. You cannot proceed with this work — contact your supervisor for replacement.',
                    )
                training_valid = True
                training_expires_at = valid.validUntil

    sig.signed = True
    sig.signedAt = datetime.now(timezone.utc)
    sig.ipAddress = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or request.headers.get("x-real-ip")
    sig.deviceInfo = request.headers.get("user-agent")
    sig.trainingValidAtSignature = training_valid
    sig.trainingExpiresAt = training_expires_at
    await db.flush()

    completed = await maybe_complete_flra(db, flra.id)
    return {"ok": True, "flraCompleted": completed}


@router.post("/{flra_id}/redo")
async def redo_flra(
    flra_id: str,
    payload: FLRARedoRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    flra = await db.get(FLRA, flra_id)
    if flra is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "FLRA not found")
    if flra.status not in {FLRAStatus.IN_PROGRESS, FLRAStatus.COMPLETED}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot re-do a {flra.status.value} FLRA.")

    # Authorisation: crew member, leader, or HSE_MANAGER / ADMIN
    sig_q = select(FLRACrewSignature).where(FLRACrewSignature.flraId == flra.id, FLRACrewSignature.userId == user.id)
    is_crew = (await db.execute(sig_q)).scalar_one_or_none() is not None or flra.leaderId == user.id
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(r in {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN"} for r in role_codes)
    if not is_crew and not is_priv:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only crew members or HSE Manager can trigger an FLRA re-do.")

    plant = await db.get(Plant, flra.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Plant lookup failed")
    last = (await db.execute(select(func.count()).select_from(FLRA).where(FLRA.plantId == flra.plantId))).scalar_one()
    new_number = f"FLRA-{plant.code}-{last + 1:04d}"

    sigs = (
        await db.execute(select(FLRACrewSignature).where(FLRACrewSignature.flraId == flra.id))
    ).scalars().all()
    crew_ids = list({s.userId for s in sigs})

    flra.status = FLRAStatus.SUPERSEDED
    flra.supersededReason = payload.reason

    # If linked permit is ACTIVE, suspend it
    if flra.permitId:
        permit = await db.get(Permit, flra.permitId)
        if permit and permit.status == PermitStatus.ACTIVE:
            permit.status = PermitStatus.SUSPENDED
            permit.suspendedAt = datetime.now(timezone.utc)
            permit.suspendedReason = f"FLRA re-do triggered: {payload.reason}"
            instance = (
                await db.execute(
                    select(WorkflowInstance).where(
                        WorkflowInstance.module == "PTW", WorkflowInstance.recordId == permit.id
                    )
                )
            ).scalar_one_or_none()
            if instance:
                db.add(
                    WorkflowHistory(
                        instanceId=instance.id,
                        stepId=instance.currentStepId,
                        stepName=instance.currentStepName or "FLRA Re-do",
                        action=Action.SUSPENDED,
                        performedById=user.id,
                        comments=f"FLRA re-do: {payload.reason}. Work suspended pending new FLRA sign-off.",
                        fromStatus="ACTIVE",
                        toStatus="SUSPENDED",
                    )
                )

    new_flra = FLRA(
        number=new_number,
        permitId=flra.permitId,
        plantId=flra.plantId,
        date=datetime.now(timezone.utc),
        location=flra.location,
        jobDescription=flra.jobDescription,
        leaderId=flra.leaderId,
        hazards=flra.hazards,
        toolboxTalkById=flra.toolboxTalkById,
        toolboxTalkConfirmed=False,
        status=FLRAStatus.IN_PROGRESS,
    )
    db.add(new_flra)
    await db.flush()
    for uid in crew_ids:
        db.add(FLRATeamMember(flraId=new_flra.id, userId=uid))
        db.add(FLRACrewSignature(flraId=new_flra.id, userId=uid))
    flra.supersededById = new_flra.id
    await db.flush()
    return {"ok": True, "newFlraId": new_flra.id, "newFlraNumber": new_flra.number}
