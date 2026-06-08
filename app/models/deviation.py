"""Deviation Management (Pharma IMS Module 1).

SQLAlchemy mirror of the Prisma `Deviation` model. The pharma equivalent of
incident management with prescriptive GMP structure: detect → QA review →
impact assessment → investigation → batch disposition → CAPA → closure. The
batch-disposition and closure steps require 21 CFR Part 11 electronic
signatures (see app/models/part11.py). Nested sub-structures are JSONB;
queryable workflow fields are promoted to columns. camelCase matches Prisma.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Deviation(Base, IdMixin):
    __tablename__ = "Deviation"

    tenantId: Mapped[str | None] = mapped_column(String)
    number: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    type: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String, nullable=False, index=True)

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    department: Mapped[str] = mapped_column(String, nullable=False, default="")
    area: Mapped[str] = mapped_column(String, nullable=False, default="")
    detectionDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrenceDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detectionMethod: Mapped[str] = mapped_column(String, nullable=False, default="")
    detectedByUserId: Mapped[str] = mapped_column(String, nullable=False)

    affectedProductName: Mapped[str | None] = mapped_column(String)
    affectedProductCode: Mapped[str | None] = mapped_column(String)
    affectedBatchNumbers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affectedBatchSize: Mapped[int | None] = mapped_column(Integer)
    batchStatusAtDetection: Mapped[str | None] = mapped_column(String)

    approvedProcessReference: Mapped[str] = mapped_column(String, nullable=False, default="")
    approvedProcessVersion: Mapped[str] = mapped_column(String, nullable=False, default="")

    immediateActionsTaken: Mapped[str] = mapped_column(Text, nullable=False, default="")
    batchQuarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    productionStopped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    qaClassifiedByUserId: Mapped[str | None] = mapped_column(String)
    qaClassifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    impactAssessment: Mapped[dict | None] = mapped_column(JSONB)

    batchDispositionRecommendation: Mapped[str | None] = mapped_column(String)
    batchDispositionJustification: Mapped[str] = mapped_column(Text, nullable=False, default="")
    batchDispositionDecidedByUserId: Mapped[str | None] = mapped_column(String)
    batchDispositionDecidedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    investigationAssignedToUserId: Mapped[str | None] = mapped_column(String, index=True)
    investigationDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    investigationExtendedDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    investigationCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rootCauseCategory: Mapped[str | None] = mapped_column(String)
    rootCauseDescription: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rootCauseMethodology: Mapped[str | None] = mapped_column(String)
    contributingFactors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    similarPastDeviations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    capaRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capaId: Mapped[str | None] = mapped_column(String, index=True)
    capaNumber: Mapped[str | None] = mapped_column(String)

    plannedDeviation: Mapped[dict | None] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String, nullable=False, default="draft", index=True)

    regulatoryReportable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    regulatoryAuthority: Mapped[str | None] = mapped_column(String)
    regulatoryReport: Mapped[dict | None] = mapped_column(JSONB)

    isRecurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    previousDeviationNumbers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    trendingTags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["Deviation"]
