"""21 CFR Part 11 / EU Annex 11 compliance primitives (pharma IMS).

Central, reusable across any GMP-regulated module. SQLAlchemy mirror of the
Prisma `ElectronicSignature` / `GmpAuditEntry` models. Both are append-only
(no updatedAt) — an electronic signature and an audit entry, once written,
are never modified. camelCase columns match Prisma exactly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class ElectronicSignature(Base, IdMixin):
    __tablename__ = "ElectronicSignature"

    tenantId: Mapped[str | None] = mapped_column(String)
    recordType: Mapped[str] = mapped_column(String, nullable=False)
    recordId: Mapped[str] = mapped_column(String, nullable=False)
    recordNumber: Mapped[str | None] = mapped_column(String)
    # Signer identity captured at signing time (snapshot, never referenced).
    signerUserId: Mapped[str] = mapped_column(String, nullable=False)
    signerFullName: Mapped[str] = mapped_column(String, nullable=False)
    signerRole: Mapped[str] = mapped_column(String, nullable=False)
    signerDepartment: Mapped[str | None] = mapped_column(String)
    signedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    signatureMeaning: Mapped[str] = mapped_column(String, nullable=False)
    ipAddress: Mapped[str | None] = mapped_column(String)
    reAuthenticated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    authenticationMethod: Mapped[str] = mapped_column(String, nullable=False, default="password")
    recordHash: Mapped[str] = mapped_column(String, nullable=False)
    signatureHash: Mapped[str] = mapped_column(String, nullable=False)
    isValid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GmpAuditEntry(Base, IdMixin):
    __tablename__ = "GmpAuditEntry"

    tenantId: Mapped[str | None] = mapped_column(String)
    recordType: Mapped[str] = mapped_column(String, nullable=False)
    recordId: Mapped[str] = mapped_column(String, nullable=False)
    recordNumber: Mapped[str | None] = mapped_column(String)
    eventType: Mapped[str] = mapped_column(String, nullable=False)
    eventAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    eventByUserId: Mapped[str] = mapped_column(String, nullable=False)
    eventByFullName: Mapped[str] = mapped_column(String, nullable=False)
    eventByRole: Mapped[str | None] = mapped_column(String)
    fieldName: Mapped[str | None] = mapped_column(String)
    oldValue: Mapped[str | None] = mapped_column(Text)
    newValue: Mapped[str | None] = mapped_column(Text)
    reasonForChange: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sessionId: Mapped[str | None] = mapped_column(String)
    ipAddress: Mapped[str | None] = mapped_column(String)
    userAgent: Mapped[str | None] = mapped_column(String)
    entryHash: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


__all__ = ["ElectronicSignature", "GmpAuditEntry"]
