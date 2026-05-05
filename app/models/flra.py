from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class FLRAStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class FLRA(Base, IdMixin):
    __tablename__ = "FLRA"
    __table_args__ = (Index("ix_flra_permit_status", "permitId", "status"),)

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    permitId: Mapped[str | None] = mapped_column(ForeignKey("Permit.id"))
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    jobDescription: Mapped[str] = mapped_column(Text, nullable=False)
    leaderId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    hazards: Mapped[str] = mapped_column(Text, nullable=False)  # JSON string
    toolboxTalkById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    toolboxTalkConfirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[FLRAStatus] = mapped_column(
        Enum(FLRAStatus, name="FLRAStatus", native_enum=False), nullable=False, default=FLRAStatus.IN_PROGRESS
    )
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersededById: Mapped[str | None] = mapped_column(ForeignKey("FLRA.id"))
    supersededReason: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    teamMembers: Mapped[list[FLRATeamMember]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )
    crewSignatures: Mapped[list[FLRACrewSignature]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )


class FLRATeamMember(Base, IdMixin):
    __tablename__ = "FLRATeamMember"
    __table_args__ = (UniqueConstraint("flraId", "userId", name="uq_flra_team"),)

    flraId: Mapped[str] = mapped_column(ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False)
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    flra: Mapped[FLRA] = relationship(back_populates="teamMembers")


class FLRACrewSignature(Base, IdMixin):
    __tablename__ = "FLRACrewSignature"
    __table_args__ = (UniqueConstraint("flraId", "userId", name="uq_flra_signature"),)

    flraId: Mapped[str] = mapped_column(ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False)
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    signed: Mapped[bool] = mapped_column(Boolean, default=False)
    signedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ipAddress: Mapped[str | None] = mapped_column(String)
    deviceInfo: Mapped[str | None] = mapped_column(String)
    trainingValidAtSignature: Mapped[bool] = mapped_column(Boolean, default=True)
    trainingExpiresAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    flra: Mapped[FLRA] = relationship(back_populates="crewSignatures")
