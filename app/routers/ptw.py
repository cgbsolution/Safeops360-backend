from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.permit import Permit, PermitStatus, PermitType
from app.models.plant import Plant
from app.models.training import TrainingProgram, TrainingRecord
from app.models.user import User
from app.models.workflow import Action, WorkflowHistory, WorkflowInstance
from app.schemas.permit import (
    AdminResetRequest,
    PermitCreate,
    PermitOut,
    ResumeRequest,
    SuspendRequest,
)
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
    get_user_role_codes,
)

router = APIRouter(prefix="/api/ptw", tags=["ptw"])

# Permit-type → required training program code. Mirror of Node side.
REQUIRED_TRAINING_CODES: dict[str, str] = {
    "HOT_WORK": "TR-HW-01",
    "CONFINED_SPACE": "TR-CSE-01",
    "WORK_AT_HEIGHT": "TR-WAH-01",
    "ELECTRICAL_LOTO": "TR-LOTO-01",
}

PERMIT_TYPE_CODE: dict[str, str] = {
    "HOT_WORK": "HW",
    "CONFINED_SPACE": "CS",
    "WORK_AT_HEIGHT": "WAH",
    "EXCAVATION": "EXC",
    "ELECTRICAL_LOTO": "ELE",
    "GENERAL_COLD": "GC",
}


@router.get("")
async def list_permits(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "PTW.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Permit)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(Permit.plantId.in_(plants))
    if read_check.matched_scope == "OWN_RECORDS":
        # Workers see permits they originated, issued, received, or are crew on.
        # Crew membership requires a join — handled via subquery below.
        from app.models.permit import PermitCrewMember
        crew_subq = select(PermitCrewMember.permitId).where(PermitCrewMember.userId == user.id)
        stmt = stmt.where(
            (Permit.originatorId == user.id)
            | (Permit.issuerId == user.id)
            | (Permit.receiverId == user.id)
            | (Permit.id.in_(crew_subq))
        )
    rows = (await db.execute(stmt.order_by(Permit.createdAt.desc()).limit(100))).scalars().all()
    return {"items": [PermitOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=PermitOut, status_code=status.HTTP_201_CREATED)
async def create_permit(
    payload: PermitCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    create_check = await can(db, user.id, "PTW.CREATE", PermissionContext(plant_id=payload.plantId))
    if not create_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, create_check.reason or "Access denied")

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    if payload.issuerId == payload.receiverId:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Issuer and receiver cannot be the same person.")
    if payload.issuerId == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Originator cannot be their own issuer.")

    issuer = await db.get(User, payload.issuerId)
    receiver = await db.get(User, payload.receiverId)
    if issuer is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid issuer")
    if receiver is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid receiver")

    # Training validity check on receiver — Hot Work / Confined Space / WAH / LOTO
    required_code = REQUIRED_TRAINING_CODES.get(payload.type.value)
    if required_code:
        prog = (
            await db.execute(select(TrainingProgram).where(TrainingProgram.code == required_code))
        ).scalar_one_or_none()
        if prog is not None:
            now = datetime.now(timezone.utc)
            tr_stmt = (
                select(TrainingRecord)
                .where(TrainingRecord.employeeId == payload.receiverId)
                .where(TrainingRecord.programId == prog.id)
                .where(TrainingRecord.passed == True)
                .where(TrainingRecord.validUntil > now)
                .order_by(TrainingRecord.validUntil.desc())
                .limit(1)
            )
            valid = (await db.execute(tr_stmt)).scalar_one_or_none()
            if valid is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f'Receiver {receiver.name} does not have a valid "{prog.name}" certification. Required for {payload.type.value} permits.',
                )

    # Validity window
    if payload.validTo <= payload.validFrom:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Valid To must be later than Valid From.")
    if payload.validTo.timestamp() < datetime.now(timezone.utc).timestamp() - 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Valid To cannot be in the past.")
    is_high_risk = payload.type.value in {"HOT_WORK", "CONFINED_SPACE"}
    max_hours = 24 if is_high_risk else 72
    duration_h = (payload.validTo - payload.validFrom).total_seconds() / 3600.0
    if duration_h > max_hours:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Validity window exceeds {max_hours}h cap for this permit type."
        )

    type_code = PERMIT_TYPE_CODE.get(payload.type.value, "PTW")
    last = (
        await db.execute(select(func.count()).select_from(Permit).where(Permit.plantId == payload.plantId))
    ).scalar_one()
    number = f"PTW-{plant.code}-{last + 1:05d}"

    permit = Permit(
        number=number,
        type=payload.type,
        plantId=payload.plantId,
        areaId=payload.areaId,
        location=payload.location,
        scopeOfWork=payload.scopeOfWork,
        validFrom=payload.validFrom,
        validTo=payload.validTo,
        originatorId=user.id,
        issuerId=payload.issuerId,
        receiverId=payload.receiverId,
        contractorName=payload.contractorName,
        isolationsRequired=payload.isolationsRequired,
        ppeChecklist=payload.ppeChecklist,
        gasTestRequired=payload.gasTestRequired,
        gasTestResult=payload.gasTestResult,
        o2Level=payload.o2Level,
        lelLevel=payload.lelLevel,
        h2sLevel=payload.h2sLevel,
        fireWatchRequired=payload.fireWatchRequired,
        rescuePlan=payload.rescuePlan,
        status=PermitStatus.DRAFT,
    )
    db.add(permit)
    await db.flush()

    try:
        await workflow_engine.initiate(
            db,
            module="PTW",
            record_id=permit.id,
            record_number=permit.number,
            record_title=permit.scopeOfWork[:120],
            record_data={
                "type": permit.type.value,
                "plantId": permit.plantId,
                "originatorId": permit.originatorId,
                "issuerId": permit.issuerId,
                "receiverId": permit.receiverId,
            },
            initiator_id=user.id,
            plant_id=permit.plantId,
        )
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"PTW workflow init failed: {e}", file=sys.stderr)

    return PermitOut.model_validate(permit)


@router.get("/{permit_id}", response_model=PermitOut)
async def get_permit(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {
        "originatorId": permit.originatorId,
        "issuerId": permit.issuerId,
        "receiverId": permit.receiverId,
    }
    result = await can(
        db, user.id, "PTW.READ",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return PermitOut.model_validate(permit)


@router.patch("/{permit_id}", response_model=PermitOut)
async def admin_reset(
    permit_id: str,
    payload: AdminResetRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermitOut:
    """Admin override — reset stuck records to DRAFT or SUBMITTED only."""
    result = await can(db, user.id, "CONFIGURATION.WORKFLOWS", PermissionContext())
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Admin only")
    if payload.status not in {"DRAFT", "SUBMITTED"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Admin override only supports DRAFT or SUBMITTED.")
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    permit.status = PermitStatus(payload.status)
    await db.flush()
    return PermitOut.model_validate(permit)


@router.post("/{permit_id}/suspend")
async def suspend_permit(
    permit_id: str,
    payload: SuspendRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {"originatorId": permit.originatorId, "issuerId": permit.issuerId, "receiverId": permit.receiverId}
    result = await can(
        db, user.id, "PTW.UPDATE",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if permit.status != PermitStatus.ACTIVE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Only ACTIVE permits can be suspended (current: {permit.status.value}).")

    permit.status = PermitStatus.SUSPENDED
    permit.suspendedAt = datetime.now(timezone.utc)
    permit.suspendedReason = payload.reason
    instance = (
        await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.module == "PTW", WorkflowInstance.recordId == permit_id)
        )
    ).scalar_one_or_none()
    if instance:
        db.add(
            WorkflowHistory(
                instanceId=instance.id,
                stepId=instance.currentStepId,
                stepName=instance.currentStepName or "Suspended",
                action=Action.ESCALATED,
                performedById=user.id,
                comments=f"Permit suspended by HSE: {payload.reason}",
                fromStatus="ACTIVE",
                toStatus="SUSPENDED",
            )
        )
    await db.flush()
    return {"ok": True}


@router.post("/{permit_id}/resume")
async def resume_permit(
    permit_id: str,
    payload: ResumeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    record = {"originatorId": permit.originatorId, "issuerId": permit.issuerId, "receiverId": permit.receiverId}
    result = await can(
        db, user.id, "PTW.UPDATE",
        PermissionContext(record_id=permit.id, plant_id=permit.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if permit.status != PermitStatus.SUSPENDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Only SUSPENDED permits can be resumed (current: {permit.status.value}).")
    if permit.validTo.timestamp() < datetime.now(timezone.utc).timestamp():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Validity window has expired. Request an extension before resuming.")
    permit.status = PermitStatus.ACTIVE
    permit.suspendedAt = None
    permit.suspendedReason = None

    instance = (
        await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.module == "PTW", WorkflowInstance.recordId == permit_id)
        )
    ).scalar_one_or_none()
    if instance:
        comments = f"Permit resumed after suspension: {payload.comments}" if payload.comments else "Permit resumed after suspension."
        db.add(
            WorkflowHistory(
                instanceId=instance.id,
                stepId=instance.currentStepId,
                stepName=instance.currentStepName or "Resumed",
                action=Action.APPROVED,
                performedById=user.id,
                comments=comments,
                fromStatus="SUSPENDED",
                toStatus="ACTIVE",
            )
        )
    await db.flush()
    return {"ok": True}


@router.get("/eligible-for-flra/list")
async def eligible_for_flra(
    q: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permits the caller can attach a fresh FLRA to. Drives the FLRA form's
    linked-permit picker."""
    eligible_statuses = [
        PermitStatus.ISSUER_APPROVED,
        PermitStatus.SAFETY_APPROVED,
        PermitStatus.PLANT_HEAD_APPROVED,
        PermitStatus.ACTIVE,
    ]
    stmt = select(Permit).where(Permit.status.in_(eligible_statuses))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Permit.number.ilike(like))
            | (Permit.location.ilike(like))
            | (Permit.scopeOfWork.ilike(like))
        )
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(r in {"HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN", "CORPORATE_HSE"} for r in role_codes)
    if not is_priv:
        from app.models.permit import PermitCrewMember
        crew_subq = select(PermitCrewMember.permitId).where(PermitCrewMember.userId == user.id)
        stmt = stmt.where(
            (Permit.receiverId == user.id)
            | (Permit.originatorId == user.id)
            | (Permit.issuerId == user.id)
            | (Permit.id.in_(crew_subq))
        )
    rows = (await db.execute(stmt.order_by(Permit.validFrom.desc()).limit(50))).scalars().all()
    return {"items": [PermitOut.model_validate(r) for r in rows]}
