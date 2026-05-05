from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class TrainingProgram(Base, IdMixin):
    __tablename__ = "TrainingProgram"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    durationHours: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    validityMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    category: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str | None] = mapped_column(ForeignKey("Plant.id"))
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)

    # Prisma's TrainingProgram only has createdAt (no updatedAt). Don't add
    # updatedAt here or queries will fail with "column does not exist".
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrainingRecord(Base, IdMixin):
    __tablename__ = "TrainingRecord"

    employeeId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    programId: Mapped[str] = mapped_column(ForeignKey("TrainingProgram.id"), nullable=False, index=True)
    trainerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    trainerName: Mapped[str | None] = mapped_column(String)

    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    durationHours: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    validUntil: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    certificateUrl: Mapped[str | None] = mapped_column(String)
    remarks: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
