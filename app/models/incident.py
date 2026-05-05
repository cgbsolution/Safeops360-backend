from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin
from app.models.user import User


class IncidentType(str, enum.Enum):
    FIRST_AID = "FIRST_AID"
    MTC = "MTC"
    RWC = "RWC"
    LTI = "LTI"
    FATALITY = "FATALITY"
    PROPERTY_DAMAGE = "PROPERTY_DAMAGE"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    FIRE = "FIRE"
    HIPO_NEAR_MISS = "HIPO_NEAR_MISS"


class IncidentStatus(str, enum.Enum):
    REPORTED = "REPORTED"
    INVESTIGATION = "INVESTIGATION"
    CAPA_ASSIGNED = "CAPA_ASSIGNED"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"


class Incident(Base, IdMixin):
    __tablename__ = "Incident"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[IncidentType] = mapped_column(Enum(IncidentType, name="IncidentType", native_enum=False), nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    location: Mapped[str] = mapped_column(String, nullable=False)
    reporterId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    injuredPersonName: Mapped[str | None] = mapped_column(String)
    injuredPersonAge: Mapped[int | None] = mapped_column(Integer)
    injuredPersonDesignation: Mapped[str | None] = mapped_column(String)
    bodyPart: Mapped[str | None] = mapped_column(String)
    natureOfInjury: Mapped[str | None] = mapped_column(String)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    immediateCause: Mapped[str | None] = mapped_column(Text)
    rootCauseMethod: Mapped[str | None] = mapped_column(String)
    rootCauseDetail: Mapped[str | None] = mapped_column(Text)
    rootCauseData: Mapped[dict | None] = mapped_column(JSON)
    rootCauseSummary: Mapped[str | None] = mapped_column(Text)
    correctiveActions: Mapped[str | None] = mapped_column(Text)
    preventiveActions: Mapped[str | None] = mapped_column(Text)

    lostDays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    propertyDamageCost: Mapped[float | None] = mapped_column(Numeric(12, 2))

    fromNearMissId: Mapped[str | None] = mapped_column(ForeignKey("NearMiss.id"))

    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, name="IncidentStatus", native_enum=False), nullable=False, default=IncidentStatus.REPORTED
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    investigationTeam: Mapped[list[IncidentInvestigationMember]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    attachments: Mapped[list[IncidentAttachment]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class IncidentInvestigationMember(Base, IdMixin):
    __tablename__ = "IncidentInvestigationMember"
    __table_args__ = (UniqueConstraint("incidentId", "userId", name="uq_incident_member"),)

    incidentId: Mapped[str] = mapped_column(ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False)
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="MEMBER")  # LEAD | MEMBER

    incident: Mapped[Incident] = relationship(back_populates="investigationTeam")


class IncidentAttachment(Base, IdMixin):
    __tablename__ = "IncidentAttachment"

    incidentId: Mapped[str] = mapped_column(ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    capaRef: Mapped[str | None] = mapped_column(String)
    witnessRef: Mapped[str | None] = mapped_column(String)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    exifData: Mapped[dict | None] = mapped_column(JSON)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    incident: Mapped[Incident] = relationship(back_populates="attachments")
    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")
