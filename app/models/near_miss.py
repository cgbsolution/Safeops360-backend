from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin
from app.models.observation import Severity


class NearMissStatus(str, enum.Enum):
    REPORTED = "REPORTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    PROMOTED = "PROMOTED"  # promoted to incident
    ACTION_OPEN = "ACTION_OPEN"
    ACTION_DONE = "ACTION_DONE"
    CLOSED = "CLOSED"


class NearMiss(Base, IdMixin):
    __tablename__ = "NearMiss"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    location: Mapped[str | None] = mapped_column(String)
    reporterId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    actionOwnerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    description: Mapped[str] = mapped_column(Text, nullable=False)
    activity: Mapped[str | None] = mapped_column(String)
    potentialSeverity: Mapped[Severity] = mapped_column(Enum(Severity, name="Severity", native_enum=False), nullable=False)
    potentialConsequence: Mapped[str] = mapped_column(String, nullable=False)
    rootCauseCategory: Mapped[str | None] = mapped_column(String)
    rootCauseDetail: Mapped[str | None] = mapped_column(Text)
    correctiveActions: Mapped[str | None] = mapped_column(Text)
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promotedToIncidentId: Mapped[str | None] = mapped_column(ForeignKey("Incident.id"))

    status: Mapped[NearMissStatus] = mapped_column(
        Enum(NearMissStatus, name="NearMissStatus", native_enum=False), nullable=False, default=NearMissStatus.REPORTED
    )
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
