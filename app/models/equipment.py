from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship


from app.models._base import Base, IdMixin


class InspectionFrequency(str, enum.Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALFYEARLY = "HALFYEARLY"
    YEARLY = "YEARLY"


class InspectionStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    DUE = "DUE"
    OVERDUE = "OVERDUE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


class Equipment(Base, IdMixin):
    __tablename__ = "Equipment"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    inspectionFrequency: Mapped[InspectionFrequency] = mapped_column(
        Enum(InspectionFrequency, name="InspectionFrequency", native_enum=False), default=InspectionFrequency.MONTHLY
    )
    isCritical: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    inspections: Mapped[list[Inspection]] = relationship(back_populates="equipment")


class Inspection(Base, IdMixin):
    __tablename__ = "Inspection"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    equipmentId: Mapped[str] = mapped_column(ForeignKey("Equipment.id"), nullable=False, index=True)
    inspectorId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checklistResult: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String)
    observations: Mapped[str | None] = mapped_column(Text)
    followUpRequired: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[InspectionStatus] = mapped_column(
        Enum(InspectionStatus, name="InspectionStatus", native_enum=False), default=InspectionStatus.SCHEDULED
    )
    # Prisma's Inspection only has createdAt (no updatedAt). Don't add
    # updatedAt here or queries will fail with "column does not exist".
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    equipment: Mapped[Equipment] = relationship(back_populates="inspections")
