"""Master / lookup tables — Department, ContractorCompany, MasterItem.
Mirrors the Prisma models added in the Near Miss refactor (Commit 1).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Department(Base, IdMixin):
    __tablename__ = "Department"
    __table_args__ = (UniqueConstraint("plantId", "name", name="Department_plantId_name_key"),)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContractorCompany(Base, IdMixin):
    __tablename__ = "ContractorCompany"

    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    code: Mapped[str | None] = mapped_column(String, unique=True)
    contactPerson: Mapped[str | None] = mapped_column(String)
    contactEmail: Mapped[str | None] = mapped_column(String)
    contactPhone: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class MasterItem(Base, IdMixin):
    """Generic key-value lookup. `type` discriminates between SHIFT,
    ACTIVITY_TYPE, HAZARD_CATEGORY, ENERGY_SOURCE, ROOT_CAUSE_CATEGORY."""

    __tablename__ = "MasterItem"
    __table_args__ = (UniqueConstraint("type", "code", name="MasterItem_type_code_key"),)

    type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
