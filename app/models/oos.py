"""OOS Investigation (Pharma IMS Module 3).

SQLAlchemy mirror of the Prisma `OosInvestigation` model. Two-phase FDA OOS
protocol: Phase 1 (lab) and, if no lab error, Phase 2 (manufacturing — spawns a
Deviation). Phase detail is JSONB; key fields are promoted to columns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class OosInvestigation(Base, IdMixin):
    __tablename__ = "OosInvestigation"

    tenantId: Mapped[str | None] = mapped_column(String)
    number: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    productName: Mapped[str] = mapped_column(String, nullable=False)
    batchNumber: Mapped[str] = mapped_column(String, nullable=False)
    testName: Mapped[str] = mapped_column(String, nullable=False)
    specificationReference: Mapped[str] = mapped_column(String, nullable=False, default="")
    specificationLimit: Mapped[str] = mapped_column(String, nullable=False, default="")
    initialResult: Mapped[str] = mapped_column(String, nullable=False)
    initialResultNumeric: Mapped[float | None] = mapped_column(Float)
    resultUnit: Mapped[str] = mapped_column(String, nullable=False, default="")
    analystUserId: Mapped[str] = mapped_column(String, nullable=False)
    analysisDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrumentId: Mapped[str | None] = mapped_column(String)

    phase1: Mapped[dict | None] = mapped_column(JSONB)
    phase1Conclusion: Mapped[str | None] = mapped_column(String)
    phase1ByUserId: Mapped[str | None] = mapped_column(String)
    phase1CompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    phase2: Mapped[dict | None] = mapped_column(JSONB)
    phase2Conclusion: Mapped[str | None] = mapped_column(String)
    phase2ByUserId: Mapped[str | None] = mapped_column(String)
    phase2CompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deviationRaised: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deviationId: Mapped[str | None] = mapped_column(String, index=True)
    deviationNumber: Mapped[str | None] = mapped_column(String)

    rootCauseCategory: Mapped[str | None] = mapped_column(String)
    rootCauseDescription: Mapped[str] = mapped_column(Text, nullable=False, default="")

    batchDisposition: Mapped[str | None] = mapped_column(String)
    batchDispositionJustification: Mapped[str] = mapped_column(Text, nullable=False, default="")
    batchDispositionByUserId: Mapped[str | None] = mapped_column(String)
    batchDispositionAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="phase_1_in_progress", index=True)

    createdByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["OosInvestigation"]
