"""Facilities service layer — shared by the factory router.

Owns the genuinely-shared behaviour:
  • sequential factory-code generation (FAC-0001, mirrors the CAMS convention)
  • buildingCount sync (recompute from active Building rows; manual when none)
  • DRAFT→ACTIVE completeness check (the ≥1-workforce gate lands in Phase B)

Cross-module references (Plant) are plain ids with no hard FK, so absence
degrades to an empty field rather than an error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import Building, FactoryProfile, WorkforceComposition

# Re-use the CAMS batch name helper — same DB, same Plant table.
from app.services.cams import plant_name_map  # noqa: F401  (re-exported for the router)


# ── code generation (tenant-scoped sequential) ──────────────────────────────
async def next_factory_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(FactoryProfile))).scalar() or 0
    return f"FAC-{(n + 1):04d}"


# ── buildingCount sync ───────────────────────────────────────────────────────
async def recompute_building_count(db: AsyncSession, profile_id: str) -> int:
    """Recompute FactoryProfile.buildingCount from the count of active, non-deleted
    Building rows. The manual count is preserved ONLY for a profile that has never
    had any Building row (greenfield); once buildings have been managed via the
    register the count tracks the active total — including dropping to 0 when the
    last building is removed (TF-02)."""
    active = (
        await db.execute(
            select(func.count())
            .select_from(Building)
            .where(Building.factoryProfileId == profile_id)
            .where(Building.isActive.is_(True))
            .where(Building.isDeleted.is_(False))
        )
    ).scalar() or 0
    # any Building row ever attached (incl. soft-deleted) ⇒ register is in use
    ever = (
        await db.execute(
            select(func.count()).select_from(Building).where(Building.factoryProfileId == profile_id)
        )
    ).scalar() or 0
    profile = await db.get(FactoryProfile, profile_id)
    if profile and ever > 0:
        profile.buildingCount = active
    return active


# ── DRAFT → ACTIVE completeness ──────────────────────────────────────────────
async def compute_profile_status(db: AsyncSession, profile: FactoryProfile) -> str:
    """Completeness (build prompt F-03 §6): name + site link + location + ≥1
    current workforce record ⇒ ACTIVE, else DRAFT. A profile already flagged
    REVIEW_DUE is left as-is."""
    if profile.profileStatus == "REVIEW_DUE":
        return "REVIEW_DUE"
    has_location = bool((profile.state or "").strip() or (profile.city or "").strip() or (profile.addressLine or "").strip())
    # A *meaningful* current workforce record (>0 headcount) is required — an
    # all-zero record shouldn't promote a profile to ACTIVE.
    has_workforce = (
        await db.execute(
            select(func.count())
            .select_from(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == profile.id)
            .where(WorkforceComposition.isCurrent.is_(True))
            .where(WorkforceComposition.isDeleted.is_(False))
            .where(WorkforceComposition.totalCount > 0)
        )
    ).scalar() or 0
    if profile.factoryName and profile.siteId and has_location and has_workforce > 0:
        return "ACTIVE"
    return "DRAFT"


# ── workforce reconciliation + history ───────────────────────────────────────
def reconcile_workforce(permanent: int, contract: int, apprentice: int, male: int, female: int, other: int) -> tuple[int, bool]:
    """Returns (totalCount, genderMismatch). totalCount is the authoritative sum
    of employment-type counts (so permanent+contract+apprentice = totalCount is
    enforced by construction). A gender split that doesn't reconcile to totalCount
    is a SOFT warning, not a block (data completeness varies)."""
    total = permanent + contract + apprentice
    gender_total = male + female + other
    return total, gender_total != total


# ── certification status engine (TF-04) ──────────────────────────────────────
def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_cert_status(expiry: datetime | None, renewal_lead_days: int | None, stored: str | None = None) -> str:
    """Effective cert status. UNDER_RENEWAL / SUSPENDED are manual overrides and
    pass through; VALID / EXPIRING_SOON / EXPIRED are always derived from the
    expiry date + renewalLeadDays (default 60), so the dashboard stays correct
    without a cron."""
    if stored in ("UNDER_RENEWAL", "SUSPENDED"):
        return stored
    if expiry is None:
        return "VALID"
    now = datetime.now(timezone.utc)
    exp = _aware(expiry)
    if exp < now:
        return "EXPIRED"
    # `0` is a valid lead ("alert only once expired") — don't fall back to 60.
    lead = 60 if renewal_lead_days is None else max(0, renewal_lead_days)
    if exp <= now + timedelta(days=lead):
        return "EXPIRING_SOON"
    return "VALID"


def cert_days_to_expiry(expiry: datetime | None) -> int | None:
    if expiry is None:
        return None
    return (_aware(expiry) - datetime.now(timezone.utc)).days


def cert_is_expiring(status: str) -> bool:
    return status in ("EXPIRING_SOON", "EXPIRED")


async def make_workforce_current(db: AsyncSession, profile: FactoryProfile, comp: WorkforceComposition) -> None:
    """Flip every other composition for this profile to historical, mark `comp`
    current, and write the denormalised headcount onto the profile."""
    prior = (
        await db.execute(
            select(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == profile.id)
            .where(WorkforceComposition.id != comp.id)
            .where(WorkforceComposition.isCurrent.is_(True))
        )
    ).scalars().all()
    for p in prior:
        p.isCurrent = False
    comp.isCurrent = True
    profile.totalEmployees = comp.totalCount
