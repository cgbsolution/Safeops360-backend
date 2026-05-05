from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.training import TrainingProgram, TrainingRecord
from app.models.user import User
from app.schemas.training import TrainingCreate, TrainingProgramOut, TrainingRecordOut

router = APIRouter(prefix="/api/training", tags=["training"])


@router.get("/programs")
async def list_programs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(select(TrainingProgram).where(TrainingProgram.isActive == True).order_by(TrainingProgram.name))
    ).scalars().all()
    return {"items": [TrainingProgramOut.model_validate(r) for r in rows]}


@router.get("")
async def list_records(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(select(TrainingRecord).order_by(TrainingRecord.date.desc()).limit(200))
    ).scalars().all()
    return {"items": [TrainingRecordOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=TrainingRecordOut, status_code=status.HTTP_201_CREATED)
async def create_record(
    payload: TrainingCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingRecordOut:
    employee = await db.get(User, payload.employeeId)
    if employee is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid employee")
    await require_permission_with_context("TRAINING.CREATE", user, db, plant_id=employee.plantId)

    program = await db.get(TrainingProgram, payload.programId)
    if program is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid program")

    if not (payload.trainerId or payload.trainerName):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Trainer is required.")
    if payload.trainerId:
        trainer = await db.get(User, payload.trainerId)
        if trainer is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid trainer")

    if payload.date.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Training date cannot be in the future.")
    if payload.score is not None and not (0 <= payload.score <= 100):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Score must be 0-100.")

    valid_until = payload.date + timedelta(days=program.validityMonths * 30)

    record = TrainingRecord(
        employeeId=payload.employeeId,
        programId=payload.programId,
        trainerId=payload.trainerId,
        trainerName=payload.trainerName,
        date=payload.date,
        durationHours=payload.durationHours,
        score=payload.score,
        passed=payload.passed,
        validUntil=valid_until,
        certificateUrl=payload.certificateUrl,
        remarks=payload.remarks,
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return TrainingRecordOut.model_validate(record)
