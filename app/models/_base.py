"""Helpers shared by all models. Kept as `_base` (leading underscore) so it's
not re-exported from `app.models`.

Prisma uses cuid() defaults; Postgres-side we use a Python uuid4 hex by default
to keep IDs collision-free across the migration window. Existing rows keep
their cuid IDs — this only affects new inserts after the cutover.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def gen_id() -> str:
    return uuid4().hex


class TimestampMixin:
    # camelCase to match Prisma's column naming convention. The DB has
    # `createdAt` / `updatedAt`; without these names matching, SQLAlchemy
    # generates `created_at` / `updated_at` SQL → Postgres "column does
    # not exist".
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # `server_default` (DB-side) on INSERT — SQLAlchemy uses RETURNING to
    # load the value into memory, so a subsequent `model_validate(obj)`
    # never lazy-loads (which would trip MissingGreenlet under async).
    # `onupdate=func.now()` keeps the value fresh on every UPDATE.
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class IdMixin:
    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)


__all__ = ["Base", "IdMixin", "TimestampMixin", "gen_id"]
