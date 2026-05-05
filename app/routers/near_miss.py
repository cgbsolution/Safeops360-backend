from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.near_miss import NearMiss, NearMissStatus
from app.models.observation import Severity
from app.models.plant import Plant
from app.models.user import User
from app.schemas.near_miss import NearMissCreate, NearMissOut, NearMissUpdate
from app.services import workflow_engine
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/near-miss", tags=["near-miss"])


@router.get("")
async def list_near_misses(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "NEAR_MISS.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(NearMiss)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(NearMiss.plantId.in_(plants))
    if read_check.matched_scope == "OWN_RECORDS":
        stmt = stmt.where((NearMiss.reporterId == user.id) | (NearMiss.actionOwnerId == user.id))
    rows = (await db.execute(stmt.order_by(NearMiss.date.desc()).limit(100))).scalars().all()
    return {"items": [NearMissOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=NearMissOut, status_code=status.HTTP_201_CREATED)
async def create_near_miss(
    payload: NearMissCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NearMissOut:
    await require_permission_with_context("NEAR_MISS.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")
    if payload.date.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Near-miss date cannot be in the future.")

    last = (
        await db.execute(select(func.count()).select_from(NearMiss).where(NearMiss.plantId == payload.plantId))
    ).scalar_one()
    number = f"NM-{payload.date.year}-{plant.code}-{last + 1:04d}"

    nm = NearMiss(
        number=number,
        date=payload.date,
        plantId=payload.plantId,
        areaId=payload.areaId,
        location=payload.location,
        reporterId=user.id,
        description=payload.description,
        activity=payload.activity,
        potentialSeverity=payload.potentialSeverity,
        potentialConsequence=payload.potentialConsequence,
        status=NearMissStatus.REPORTED,
    )
    db.add(nm)
    await db.flush()
    await db.refresh(nm)

    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="NEAR_MISS",
                record_id=nm.id,
                record_number=nm.number,
                record_title=nm.description[:120],
                record_data={
                    "potentialSeverity": nm.potentialSeverity.value,
                    "plantId": nm.plantId,
                    "reporterId": nm.reporterId,
                },
                initiator_id=user.id,
                plant_id=nm.plantId,
            )
    except Exception as e:  # noqa: BLE001
        import sys
        import traceback
        print(f"Near-miss workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return NearMissOut.model_validate(nm)


@router.get("/{nm_id}", response_model=NearMissOut)
async def get_near_miss(
    nm_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NearMissOut:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.READ",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    return NearMissOut.model_validate(nm)


@router.patch("/{nm_id}", response_model=NearMissOut)
async def update_near_miss(
    nm_id: str,
    payload: NearMissUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NearMissOut:
    nm = await db.get(NearMiss, nm_id)
    if nm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if nm.status == NearMissStatus.CLOSED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot edit a closed near miss.")
    record = {"reporterId": nm.reporterId, "actionOwnerId": nm.actionOwnerId}
    result = await can(
        db, user.id, "NEAR_MISS.UPDATE",
        PermissionContext(record_id=nm.id, plant_id=nm.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    if payload.actionOwnerId is not None:
        if payload.actionOwnerId:
            owner = await db.get(User, payload.actionOwnerId)
            if owner is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid action owner")
            nm.actionOwnerId = payload.actionOwnerId
        else:
            nm.actionOwnerId = None
    if payload.correctiveActions is not None:
        nm.correctiveActions = payload.correctiveActions or None
    if payload.rootCauseCategory is not None:
        nm.rootCauseCategory = payload.rootCauseCategory or None
    if payload.rootCauseDetail is not None:
        nm.rootCauseDetail = payload.rootCauseDetail or None
    if payload.targetDate is not None:
        if payload.targetDate < datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target closure date cannot be in the past.")
        nm.targetDate = payload.targetDate
    await db.flush()
    return NearMissOut.model_validate(nm)
