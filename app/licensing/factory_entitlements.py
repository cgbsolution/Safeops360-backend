"""Per-factory (per-Plant) module allocation — the admin-managed layer that
sits *within* the signed-licence ceiling.

Model (opt-out): a small in-memory cache maps plantId → the set of module codes
an admin has DISABLED for that factory. Effective access for a factory is:

    module is usable at plant P  ==  is_module_enabled(code)        # signed ceiling
                                     AND code not in disabled[P]    # admin restriction

So this layer can only ever RESTRICT within the licence — never grant a module
the licence doesn't include (build prompt §5.3 still holds). Absence of any row
for a plant means every licensed module is on there (no regression).

The cache is refreshed on boot and after each admin save, so the hot path
(`require_module`) never hits the DB.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal

log = logging.getLogger("safeops360.licensing")

# plantId → {disabled module codes}. Only DISABLED entries are cached.
_disabled: dict[str, set[str]] = {}


def disabled_for_plant(plant_id: str | None) -> set[str]:
    if not plant_id:
        return set()
    return _disabled.get(plant_id, set())


def is_enabled_for_plant(code: str, plant_id: str | None) -> bool:
    """Per-factory restriction check ONLY (the global ceiling is checked
    separately by enforcement.is_module_enabled). True unless an admin has
    disabled `code` for this plant."""
    return code not in disabled_for_plant(plant_id)


async def refresh(db: AsyncSession | None = None) -> None:
    """Reload the disabled-overrides cache from the DB."""
    if db is not None:
        await _load(db)
        return
    try:
        async with AsyncSessionLocal() as session:
            await _load(session)
    except Exception as e:  # noqa: BLE001 — never let this crash boot
        log.warning("Factory-entitlement cache refresh failed: %s", e)


async def _load(db: AsyncSession) -> None:
    from app.models.licensing import FactoryModuleEntitlement

    rows = (
        await db.execute(
            select(FactoryModuleEntitlement).where(FactoryModuleEntitlement.enabled.is_(False))
        )
    ).scalars().all()
    fresh: dict[str, set[str]] = {}
    for r in rows:
        fresh.setdefault(r.plantId, set()).add(r.moduleCode)
    global _disabled
    _disabled = fresh
    log.info("Factory-entitlement cache: %d plant(s) with restrictions", len(fresh))


async def load_all(db: AsyncSession) -> list[dict]:
    """All explicit rows (for the admin matrix)."""
    from app.models.licensing import FactoryModuleEntitlement

    rows = (await db.execute(select(FactoryModuleEntitlement))).scalars().all()
    return [{"plantId": r.plantId, "moduleCode": r.moduleCode, "enabled": r.enabled} for r in rows]


async def set_for_plant(
    db: AsyncSession, plant_id: str, changes: dict[str, bool], updated_by: str | None
) -> None:
    """Upsert the enabled/disabled state of one or more modules for a plant.
    Caller must have already validated `changes` keys against the licence
    ceiling. Refreshes the cache afterwards."""
    from app.models.licensing import FactoryModuleEntitlement

    existing = {
        r.moduleCode: r
        for r in (
            await db.execute(
                select(FactoryModuleEntitlement).where(
                    FactoryModuleEntitlement.plantId == plant_id
                )
            )
        ).scalars().all()
    }
    for code, enabled in changes.items():
        row = existing.get(code)
        if row is None:
            db.add(
                FactoryModuleEntitlement(
                    plantId=plant_id, moduleCode=code, enabled=enabled, updatedBy=updated_by
                )
            )
        else:
            row.enabled = enabled
            row.updatedBy = updated_by
    await db.flush()
    await refresh(db)
