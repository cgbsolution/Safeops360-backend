"""Audit Management (Pharma IMS Module 4).

SQLAlchemy mirror of the Prisma `Audit` / `AuditFinding` models. Internal GMP /
supplier / regulatory audits with findings that spawn CAPAs. camelCase columns
match Prisma. (Module named audit_mgmt to avoid clashing with the AUDIT.* RBAC
module name and the generic audit-trail concept.)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class Audit(Base, IdMixin):
    __tablename__ = "Audit"

    tenantId: Mapped[str | None] = mapped_column(String)
    number: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    auditType: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    scope: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    applicableStandards: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    regulatoryAuthority: Mapped[str | None] = mapped_column(String)
    inspectionType: Mapped[str | None] = mapped_column(String)
    supplierName: Mapped[str | None] = mapped_column(String)
    supplierSite: Mapped[str | None] = mapped_column(String)

    plannedStart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plannedEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actualStart: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualEnd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    leadAuditorUserId: Mapped[str] = mapped_column(String, nullable=False)
    auditTeam: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    auditeeDepartmentHeadUserId: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="planned", index=True)
    auditReportUrl: Mapped[str | None] = mapped_column(String)
    reportIssuedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    regulatoryCommitments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    findings: Mapped[list["AuditFinding"]] = relationship(back_populates="audit", cascade="all, delete-orphan")


class AuditFinding(Base, IdMixin):
    __tablename__ = "AuditFinding"

    tenantId: Mapped[str | None] = mapped_column(String)
    auditId: Mapped[str] = mapped_column(ForeignKey("Audit.id", ondelete="CASCADE"), nullable=False, index=True)
    audit: Mapped[Audit] = relationship(back_populates="findings")

    findingNumber: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    area: Mapped[str] = mapped_column(String, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    referenceRequirement: Mapped[str] = mapped_column(String, nullable=False, default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    responseDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auditeeResponse: Mapped[str] = mapped_column(Text, nullable=False, default="")
    capaId: Mapped[str | None] = mapped_column(String)
    capaNumber: Mapped[str | None] = mapped_column(String)
    capaStatus: Mapped[str | None] = mapped_column(String)
    findingStatus: Mapped[str] = mapped_column(String, nullable=False, default="open")
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["Audit", "AuditFinding"]
