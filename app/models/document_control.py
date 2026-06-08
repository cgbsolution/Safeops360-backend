"""Document Control (Pharma IMS Module 2).

SQLAlchemy mirror of the Prisma `ControlledDocument` / `DocumentVersion`
models. The master holds the live/effective state; each version is an immutable
revision carrying its own author/review/approval signatures (21 CFR Part 11)
and file hash. camelCase columns match Prisma.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class ControlledDocument(Base, IdMixin):
    __tablename__ = "ControlledDocument"

    tenantId: Mapped[str | None] = mapped_column(String)
    documentNumber: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    documentType: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, default="")
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    currentVersion: Mapped[str] = mapped_column(String, nullable=False, default="1.0")
    currentVersionStatus: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    currentVersionEffectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currentDocumentFileUrl: Mapped[str | None] = mapped_column(String)
    currentDocumentFileHash: Mapped[str | None] = mapped_column(String)

    nextReviewDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewFrequencyMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    reviewOwnerRole: Mapped[str | None] = mapped_column(String)
    reviewOwnerUserId: Mapped[str | None] = mapped_column(String)

    applicableAreas: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    applicableRoles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    applicableProducts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    originatedFromMocId: Mapped[str | None] = mapped_column(String)
    originatedFromDeviationId: Mapped[str | None] = mapped_column(String)
    originatedFromCapaId: Mapped[str | None] = mapped_column(String)
    originatedFromAuditId: Mapped[str | None] = mapped_column(String)

    requiresTrainingOnNewVersion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trainingCompletionBeforeEffective: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linkedTrainingProgramId: Mapped[str | None] = mapped_column(String)

    distributionList: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    referencedDocuments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    regulatoryReference: Mapped[str] = mapped_column(String, nullable=False, default="")
    retentionYears: Mapped[int] = mapped_column(Integer, nullable=False, default=7)

    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base, IdMixin):
    __tablename__ = "DocumentVersion"
    __table_args__ = (
        UniqueConstraint("documentId", "version", name="DocumentVersion_documentId_version_key"),
    )

    tenantId: Mapped[str | None] = mapped_column(String)
    documentId: Mapped[str] = mapped_column(
        ForeignKey("ControlledDocument.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document: Mapped[ControlledDocument] = relationship(back_populates="versions")
    version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")

    authoredByUserId: Mapped[str] = mapped_column(String, nullable=False)
    authoredAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    technicalReviewByUserId: Mapped[str | None] = mapped_column(String)
    technicalReviewAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qaReviewByUserId: Mapped[str | None] = mapped_column(String)
    qaReviewAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedByUserId: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersededAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    changeSummary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    documentFileUrl: Mapped[str | None] = mapped_column(String)
    documentFileHash: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["ControlledDocument", "DocumentVersion"]
