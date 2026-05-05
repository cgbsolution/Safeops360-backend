from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class PermitType(str, enum.Enum):
    HOT_WORK = "HOT_WORK"
    CONFINED_SPACE = "CONFINED_SPACE"
    WORK_AT_HEIGHT = "WORK_AT_HEIGHT"
    EXCAVATION = "EXCAVATION"
    ELECTRICAL_LOTO = "ELECTRICAL_LOTO"
    GENERAL_COLD = "GENERAL_COLD"


class PermitStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    ISSUER_APPROVED = "ISSUER_APPROVED"
    SAFETY_APPROVED = "SAFETY_APPROVED"
    PLANT_HEAD_APPROVED = "PLANT_HEAD_APPROVED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class Permit(Base, IdMixin):
    __tablename__ = "Permit"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[PermitType] = mapped_column(Enum(PermitType, name="PermitType", native_enum=False), nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    location: Mapped[str] = mapped_column(String, nullable=False)
    scopeOfWork: Mapped[str] = mapped_column(Text, nullable=False)
    validFrom: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validTo: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    originatorId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    issuerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    receiverId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    contractorName: Mapped[str | None] = mapped_column(String)

    isolationsRequired: Mapped[str | None] = mapped_column(Text)
    ppeChecklist: Mapped[str | None] = mapped_column(Text)  # JSON string
    gasTestRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    gasTestResult: Mapped[str | None] = mapped_column(String)
    o2Level: Mapped[str | None] = mapped_column(String)
    lelLevel: Mapped[str | None] = mapped_column(String)
    h2sLevel: Mapped[str | None] = mapped_column(String)
    fireWatchRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    rescuePlan: Mapped[str | None] = mapped_column(Text)

    issuerApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safetyApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plantHeadApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[PermitStatus] = mapped_column(
        Enum(PermitStatus, name="PermitStatus", native_enum=False), nullable=False, default=PermitStatus.DRAFT
    )
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    suspendedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspendedReason: Mapped[str | None] = mapped_column(Text)
    expiredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    workCrew: Mapped[list[PermitCrewMember]] = relationship(
        back_populates="permit", cascade="all, delete-orphan"
    )


class PermitCrewMember(Base, IdMixin):
    __tablename__ = "PermitCrewMember"
    __table_args__ = (UniqueConstraint("permitId", "userId", name="uq_permit_crew"),)

    permitId: Mapped[str] = mapped_column(ForeignKey("Permit.id", ondelete="CASCADE"), nullable=False)
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="WORKER")
    addedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    permit: Mapped[Permit] = relationship(back_populates="workCrew")
