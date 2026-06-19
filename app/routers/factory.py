"""Facilities — Factory Profile Master router.

A descriptive/compliance layer 1:1 with the existing Plant (= "Site"). Owns
profile attributes + the building register; the consolidated dashboard reads
live operational metrics per siteId from the existing engines (Phase D). All
endpoints plant-scoped + RBAC-enforced.

Permission codes (seeded in seed-rbac.ts):
  FACILITY.READ    view profiles / buildings / consolidated dashboard
  FACILITY.CREATE  create a factory profile (1:1 with a Site)
  FACILITY.UPDATE  edit profile + manage buildings
  FACILITY.DELETE  archive a factory profile (soft delete)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.factory import (
    Building,
    FactoryCertification,
    FactoryContact,
    FactoryProfile,
    ProductionProcess,
    WorkforceComposition,
)
from app.models.user import User
from app.schemas import factory as S
from app.services import factory as svc
from app.services.permissions import PermissionContext, can, get_accessible_plants

router = APIRouter(prefix="/api/factory", tags=["factory"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _read_profile(db: AsyncSession, user: User, profile_id: str) -> FactoryProfile:
    """Fetch a profile and enforce site-scoped FACILITY.READ. `can()` is permissive
    when no plant_id is supplied (pages are expected to filter), so reads MUST pass
    the profile's siteId — otherwise a plant-scoped Factory Manager could read any
    site's data."""
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.READ", plant_id=p.siteId)
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── serialisation ────────────────────────────────────────────────────────────
def _profile_out(p: FactoryProfile, plants: dict[str, str]) -> S.FactoryProfileOut:
    o = S.FactoryProfileOut.model_validate(p)
    o.siteName = plants.get(p.siteId)
    return o


def _workforce_out(w: WorkforceComposition) -> S.WorkforceCompositionOut:
    o = S.WorkforceCompositionOut.model_validate(w)
    o.genderTotal = w.maleCount + w.femaleCount + w.otherGenderCount
    o.genderMismatch = o.genderTotal != w.totalCount
    return o


def _cert_out(c: FactoryCertification) -> S.FactoryCertificationOut:
    o = S.FactoryCertificationOut.model_validate(c)
    o.status = svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status)
    o.daysToExpiry = svc.cert_days_to_expiry(c.expiryDate)
    return o


async def _profile_detail(db: AsyncSession, p: FactoryProfile) -> S.FactoryProfileDetail:
    plants = await svc.plant_name_map(db, [p.siteId])
    buildings = (
        await db.execute(
            select(Building)
            .where(Building.factoryProfileId == p.id)
            .where(Building.isDeleted.is_(False))
            .order_by(Building.buildingName.asc())
        )
    ).scalars().all()
    workforce = (
        await db.execute(
            select(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == p.id)
            .where(WorkforceComposition.isDeleted.is_(False))
            .order_by(WorkforceComposition.asOfDate.desc())
        )
    ).scalars().all()
    processes = (
        await db.execute(
            select(ProductionProcess)
            .where(ProductionProcess.factoryProfileId == p.id)
            .where(ProductionProcess.isDeleted.is_(False))
            .order_by(ProductionProcess.sequenceOrder.asc().nulls_last(), ProductionProcess.processName.asc())
        )
    ).scalars().all()
    certs = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == p.id)
            .where(FactoryCertification.isDeleted.is_(False))
            .order_by(FactoryCertification.certificationType.asc())
        )
    ).scalars().all()
    contacts = (
        await db.execute(
            select(FactoryContact)
            .where(FactoryContact.factoryProfileId == p.id)
            .where(FactoryContact.isDeleted.is_(False))
            .order_by(FactoryContact.isPrimary.desc(), FactoryContact.name.asc())
        )
    ).scalars().all()
    # Validate the scalar base first (avoids Pydantic touching lazy
    # relationships → async lazy-load outside the greenlet), then attach the
    # explicitly-queried children.
    base = _profile_out(p, plants)
    cert_outs = [_cert_out(c) for c in certs]
    base.certCount = len(cert_outs)
    base.certsExpiringCount = sum(1 for c in cert_outs if svc.cert_is_expiring(c.status))
    current = next((w for w in workforce if w.isCurrent), None)
    return S.FactoryProfileDetail(
        **base.model_dump(),
        buildings=[S.BuildingOut.model_validate(b) for b in buildings],
        currentWorkforce=_workforce_out(current) if current else None,
        workforceHistory=[_workforce_out(w) for w in workforce],
        processes=[S.ProductionProcessOut.model_validate(pr) for pr in processes],
        certifications=cert_outs,
        contacts=[S.FactoryContactOut.model_validate(ct) for ct in contacts],
    )


# ════════════════════════════════════════════════════════════════════════════
# Profiles  (F-01 list / F-02 detail / F-03 create)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles", response_model=S.FactoryProfileListResponse)
async def list_profiles(
    state: str | None = Query(None),
    pstatus: str | None = Query(None, alias="status"),
    profileStatus: str | None = Query(None),
    siteId: str | None = Query(None),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "FACILITY.READ")
    # Plant-scope the dashboard: a Factory Manager (OWN_PLANT) sees only their
    # site(s); a group manager (ALL_PLANTS) gets None ⇒ unrestricted.
    accessible = await get_accessible_plants(db, user.id)
    if accessible is not None and not accessible:
        return S.FactoryProfileListResponse(items=[], total=0)
    stmt = select(FactoryProfile).where(FactoryProfile.isDeleted.is_(False))
    if accessible is not None:
        stmt = stmt.where(FactoryProfile.siteId.in_(accessible))
    if state:
        stmt = stmt.where(FactoryProfile.state == state)
    if pstatus:
        stmt = stmt.where(FactoryProfile.status == pstatus)
    if profileStatus:
        stmt = stmt.where(FactoryProfile.profileStatus == profileStatus)
    if siteId:
        stmt = stmt.where(FactoryProfile.siteId == siteId)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(FactoryProfile.factoryName.ilike(like), FactoryProfile.factoryCode.ilike(like), FactoryProfile.city.ilike(like)))
    rows = (await db.execute(stmt.order_by(FactoryProfile.factoryName.asc()))).scalars().all()

    plants = await svc.plant_name_map(db, [p.siteId for p in rows])

    # Batch-load certs for the listed profiles → per-profile + group cert counts.
    profile_ids = [p.id for p in rows]
    cert_by_profile: dict[str, list[FactoryCertification]] = {}
    if profile_ids:
        cert_rows = (
            await db.execute(
                select(FactoryCertification)
                .where(FactoryCertification.factoryProfileId.in_(profile_ids))
                .where(FactoryCertification.isDeleted.is_(False))
            )
        ).scalars().all()
        for c in cert_rows:
            cert_by_profile.setdefault(c.factoryProfileId, []).append(c)

    items: list[S.FactoryProfileOut] = []
    group_expiring = 0
    status_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    for p in rows:
        o = _profile_out(p, plants)
        certs = cert_by_profile.get(p.id, [])
        o.certCount = len(certs)
        o.certsExpiringCount = sum(
            1 for c in certs if svc.cert_is_expiring(svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status))
        )
        group_expiring += o.certsExpiringCount
        items.append(o)
        status_counts[p.status] = status_counts.get(p.status, 0) + 1
        if p.state:
            state_counts[p.state] = state_counts.get(p.state, 0) + 1

    return S.FactoryProfileListResponse(
        items=items,
        total=len(items),
        totalBuildings=sum(p.buildingCount for p in rows),
        totalEmployees=sum(p.totalEmployees for p in rows),
        certsExpiring=group_expiring,
        statusCounts=status_counts,
        stateCounts=state_counts,
    )


@router.post("/profiles", response_model=S.FactoryProfileDetail, status_code=201)
async def create_profile(body: S.FactoryProfileCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "FACILITY.CREATE", plant_id=body.siteId)

    # Enforce the 1:1 site mapping (TF-01) — a Site carries one profile. The DB
    # unique constraint covers soft-deleted rows too, so check ANY existing row
    # (not just active) and return a clean 409 rather than a 500 unique-violation.
    existing = (
        await db.execute(select(FactoryProfile).where(FactoryProfile.siteId == body.siteId))
    ).scalars().first()
    if existing and not existing.isDeleted:
        raise HTTPException(status.HTTP_409_CONFLICT, "A factory profile already exists for this site.")
    if existing and existing.isDeleted:
        raise HTTPException(status.HTTP_409_CONFLICT, "A factory profile for this site was archived; restore it instead of creating a new one.")

    p = FactoryProfile(
        siteId=body.siteId,
        factoryCode=body.factoryCode or await svc.next_factory_code(db),
        factoryName=body.factoryName,
        status=body.status,
        ownershipType=body.ownershipType,
        addressLine=body.addressLine,
        city=body.city,
        state=body.state,
        pincode=body.pincode,
        latitude=body.latitude,
        longitude=body.longitude,
        establishedYear=body.establishedYear,
        factoryLicenseNo=body.factoryLicenseNo,
        factoryLicenseValidUntil=body.factoryLicenseValidUntil,
        registrationNos=[r.model_dump() for r in body.registrationNos],
        applicableActs=body.applicableActs,
        pollutionControlBoard=body.pollutionControlBoard,
        totalLandAreaSqm=body.totalLandAreaSqm,
        builtUpAreaSqm=body.builtUpAreaSqm,
        buildingCount=body.buildingCount or 0,
        primaryIndustry=body.primaryIndustry,
        profileStatus="DRAFT",
        createdBy=user.id,
    )
    db.add(p)
    await db.flush()  # assign p.id before attaching buildings

    for b in body.buildings:
        db.add(Building(
            factoryProfileId=p.id, siteId=p.siteId, buildingName=b.buildingName, buildingType=b.buildingType,
            floors=b.floors, areaSqm=b.areaSqm, maxOccupancy=b.maxOccupancy, currentOccupancy=b.currentOccupancy,
            yearBuilt=b.yearBuilt, assemblyPoint=b.assemblyPoint, emergencyExits=b.emergencyExits,
            occupancyCertificateNo=b.occupancyCertificateNo, isActive=b.isActive, createdBy=user.id,
        ))
    await db.flush()
    await svc.recompute_building_count(db, p.id)

    # Initial workforce composition (optional) — reconcile + denormalise.
    if body.workforce is not None:
        w = body.workforce
        total, _mismatch = svc.reconcile_workforce(
            w.permanentCount, w.contractCount, w.apprenticeTraineeCount, w.maleCount, w.femaleCount, w.otherGenderCount
        )
        comp = WorkforceComposition(
            factoryProfileId=p.id, siteId=p.siteId, asOfDate=w.asOfDate or _now(), isCurrent=True,
            permanentCount=w.permanentCount, contractCount=w.contractCount, apprenticeTraineeCount=w.apprenticeTraineeCount,
            maleCount=w.maleCount, femaleCount=w.femaleCount, otherGenderCount=w.otherGenderCount,
            migrantWorkerCount=w.migrantWorkerCount, differentlyAbledCount=w.differentlyAbledCount,
            totalCount=total, notes=w.notes, createdBy=user.id,
        )
        db.add(comp)
        await db.flush()
        await svc.make_workforce_current(db, p, comp)

    # Initial production processes (optional).
    for idx, pr in enumerate(body.processes):
        db.add(ProductionProcess(
            factoryProfileId=p.id, siteId=p.siteId, processName=pr.processName, processCategory=pr.processCategory,
            description=pr.description, sequenceOrder=pr.sequenceOrder if pr.sequenceOrder is not None else idx + 1,
            shiftPattern=pr.shiftPattern, installedCapacity=pr.installedCapacity, keyHazards=pr.keyHazards,
            isActive=pr.isActive, createdBy=user.id,
        ))
    await db.flush()
    p.profileStatus = await svc.compute_profile_status(db, p)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A factory profile already exists for this site.")
    await db.refresh(p)
    return await _profile_detail(db, p)


@router.get("/profiles/{profile_id}", response_model=S.FactoryProfileDetail)
async def get_profile(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await _read_profile(db, user, profile_id)
    return await _profile_detail(db, p)


@router.patch("/profiles/{profile_id}", response_model=S.FactoryProfileDetail)
async def update_profile(profile_id: str, body: S.FactoryProfileUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=p.siteId)

    data = body.model_dump(exclude_unset=True)
    if "registrationNos" in data and data["registrationNos"] is not None:
        data["registrationNos"] = [r if isinstance(r, dict) else r.model_dump() for r in body.registrationNos]
    for k, v in data.items():
        setattr(p, k, v)
    p.updatedBy = user.id
    # keep profileStatus honest unless the caller explicitly set it
    if "profileStatus" not in data:
        p.profileStatus = await svc.compute_profile_status(db, p)

    await db.commit()
    await db.refresh(p)
    return await _profile_detail(db, p)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.DELETE", plant_id=p.siteId)
    p.isDeleted = True
    p.updatedBy = user.id
    # Cascade the soft-delete to children (the DB FK cascade only fires on a
    # HARD delete, so a soft-deleted profile would otherwise leave active orphans).
    for model in (Building, WorkforceComposition, ProductionProcess, FactoryCertification, FactoryContact):
        await db.execute(
            update(model)
            .where(model.factoryProfileId == p.id)
            .where(model.isDeleted.is_(False))
            .values(isDeleted=True, updatedBy=user.id)
        )
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Buildings  (F-02 Buildings tab) — each mutation re-syncs buildingCount (TF-02)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/buildings", response_model=list[S.BuildingOut])
async def list_buildings(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(Building)
            .where(Building.factoryProfileId == profile_id)
            .where(Building.isDeleted.is_(False))
            .order_by(Building.buildingName.asc())
        )
    ).scalars().all()
    return [S.BuildingOut.model_validate(b) for b in rows]


@router.post("/profiles/{profile_id}/buildings", response_model=S.BuildingOut, status_code=201)
async def create_building(profile_id: str, body: S.BuildingCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=p.siteId)
    b = Building(
        factoryProfileId=p.id, siteId=p.siteId, buildingName=body.buildingName, buildingType=body.buildingType,
        floors=body.floors, areaSqm=body.areaSqm, maxOccupancy=body.maxOccupancy, currentOccupancy=body.currentOccupancy,
        yearBuilt=body.yearBuilt, assemblyPoint=body.assemblyPoint, emergencyExits=body.emergencyExits,
        occupancyCertificateNo=body.occupancyCertificateNo, isActive=body.isActive, createdBy=user.id,
    )
    db.add(b)
    await db.flush()
    await svc.recompute_building_count(db, p.id)
    await db.commit()
    await db.refresh(b)
    return S.BuildingOut.model_validate(b)


@router.patch("/buildings/{building_id}", response_model=S.BuildingOut)
async def update_building(building_id: str, body: S.BuildingUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Building, building_id)
    if not b or b.isDeleted:
        raise HTTPException(404, "Building not found")
    p = await db.get(FactoryProfile, b.factoryProfileId)
    await _require(db, user, "FACILITY.UPDATE", plant_id=b.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(b, k, v)
    b.updatedBy = user.id
    await db.flush()
    if p:
        await svc.recompute_building_count(db, p.id)
    await db.commit()
    await db.refresh(b)
    return S.BuildingOut.model_validate(b)


@router.delete("/buildings/{building_id}", status_code=204)
async def delete_building(building_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Building, building_id)
    if not b or b.isDeleted:
        raise HTTPException(404, "Building not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=b.siteId)
    b.isDeleted = True
    b.updatedBy = user.id
    await db.flush()
    await svc.recompute_building_count(db, b.factoryProfileId)
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Workforce composition (F-02 Workforce tab) — SA8000 lens; history retained
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/workforce", response_model=list[S.WorkforceCompositionOut])
async def list_workforce(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == profile_id)
            .where(WorkforceComposition.isDeleted.is_(False))
            .order_by(WorkforceComposition.asOfDate.desc())
        )
    ).scalars().all()
    return [_workforce_out(w) for w in rows]


@router.post("/profiles/{profile_id}/workforce", response_model=S.WorkforceCompositionOut, status_code=201)
async def add_workforce(profile_id: str, body: S.WorkforceCompositionCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.WORKFORCE_UPDATE", plant_id=p.siteId)
    total, _mismatch = svc.reconcile_workforce(
        body.permanentCount, body.contractCount, body.apprenticeTraineeCount, body.maleCount, body.femaleCount, body.otherGenderCount
    )
    comp = WorkforceComposition(
        factoryProfileId=p.id, siteId=p.siteId, asOfDate=body.asOfDate or _now(), isCurrent=True,
        permanentCount=body.permanentCount, contractCount=body.contractCount, apprenticeTraineeCount=body.apprenticeTraineeCount,
        maleCount=body.maleCount, femaleCount=body.femaleCount, otherGenderCount=body.otherGenderCount,
        migrantWorkerCount=body.migrantWorkerCount, differentlyAbledCount=body.differentlyAbledCount,
        totalCount=total, notes=body.notes, createdBy=user.id,
    )
    db.add(comp)
    await db.flush()
    await svc.make_workforce_current(db, p, comp)  # flip prior → historical + write totalEmployees
    p.profileStatus = await svc.compute_profile_status(db, p)
    await db.commit()
    await db.refresh(comp)
    return _workforce_out(comp)


# ════════════════════════════════════════════════════════════════════════════
# Production processes (F-02 Production Processes tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/processes", response_model=list[S.ProductionProcessOut])
async def list_processes(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(ProductionProcess)
            .where(ProductionProcess.factoryProfileId == profile_id)
            .where(ProductionProcess.isDeleted.is_(False))
            .order_by(ProductionProcess.sequenceOrder.asc().nulls_last(), ProductionProcess.processName.asc())
        )
    ).scalars().all()
    return [S.ProductionProcessOut.model_validate(pr) for pr in rows]


@router.post("/profiles/{profile_id}/processes", response_model=S.ProductionProcessOut, status_code=201)
async def create_process(profile_id: str, body: S.ProductionProcessCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=p.siteId)
    pr = ProductionProcess(
        factoryProfileId=p.id, siteId=p.siteId, processName=body.processName, processCategory=body.processCategory,
        description=body.description, sequenceOrder=body.sequenceOrder, shiftPattern=body.shiftPattern,
        installedCapacity=body.installedCapacity, keyHazards=body.keyHazards, isActive=body.isActive, createdBy=user.id,
    )
    db.add(pr)
    await db.commit()
    await db.refresh(pr)
    return S.ProductionProcessOut.model_validate(pr)


@router.patch("/processes/{process_id}", response_model=S.ProductionProcessOut)
async def update_process(process_id: str, body: S.ProductionProcessUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    pr = await db.get(ProductionProcess, process_id)
    if not pr or pr.isDeleted:
        raise HTTPException(404, "Production process not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=pr.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(pr, k, v)
    pr.updatedBy = user.id
    await db.commit()
    await db.refresh(pr)
    return S.ProductionProcessOut.model_validate(pr)


@router.delete("/processes/{process_id}", status_code=204)
async def delete_process(process_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    pr = await db.get(ProductionProcess, process_id)
    if not pr or pr.isDeleted:
        raise HTTPException(404, "Production process not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=pr.siteId)
    pr.isDeleted = True
    pr.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Certifications (F-02 Certifications tab) — status engine (TF-04)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/certifications", response_model=list[S.FactoryCertificationOut])
async def list_certifications(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == profile_id)
            .where(FactoryCertification.isDeleted.is_(False))
            .order_by(FactoryCertification.certificationType.asc())
        )
    ).scalars().all()
    return [_cert_out(c) for c in rows]


@router.post("/profiles/{profile_id}/certifications", response_model=S.FactoryCertificationOut, status_code=201)
async def create_certification(profile_id: str, body: S.FactoryCertificationCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.CERT_MANAGE", plant_id=p.siteId)
    status_val = body.status or svc.compute_cert_status(body.expiryDate, body.renewalLeadDays, None)
    c = FactoryCertification(
        factoryProfileId=p.id, siteId=p.siteId, certificationType=body.certificationType, certificateNo=body.certificateNo,
        issuingBody=body.issuingBody, issueDate=body.issueDate, expiryDate=body.expiryDate, renewalLeadDays=body.renewalLeadDays,
        status=status_val, scopeNotes=body.scopeNotes, attachmentIds=body.attachmentIds, createdBy=user.id,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return _cert_out(c)


@router.patch("/certifications/{cert_id}", response_model=S.FactoryCertificationOut)
async def update_certification(cert_id: str, body: S.FactoryCertificationUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(FactoryCertification, cert_id)
    if not c or c.isDeleted:
        raise HTTPException(404, "Certification not found")
    await _require(db, user, "FACILITY.CERT_MANAGE", plant_id=c.siteId)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(c, k, v)
    # Recompute the cached status from dates unless the caller set it explicitly.
    if "status" not in data:
        c.status = svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, None)
    c.updatedBy = user.id
    await db.commit()
    await db.refresh(c)
    return _cert_out(c)


@router.delete("/certifications/{cert_id}", status_code=204)
async def delete_certification(cert_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(FactoryCertification, cert_id)
    if not c or c.isDeleted:
        raise HTTPException(404, "Certification not found")
    await _require(db, user, "FACILITY.CERT_MANAGE", plant_id=c.siteId)
    c.isDeleted = True
    c.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Contacts (F-02 Contacts tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/contacts", response_model=list[S.FactoryContactOut])
async def list_contacts(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(FactoryContact)
            .where(FactoryContact.factoryProfileId == profile_id)
            .where(FactoryContact.isDeleted.is_(False))
            .order_by(FactoryContact.isPrimary.desc(), FactoryContact.name.asc())
        )
    ).scalars().all()
    return [S.FactoryContactOut.model_validate(ct) for ct in rows]


@router.post("/profiles/{profile_id}/contacts", response_model=S.FactoryContactOut, status_code=201)
async def create_contact(profile_id: str, body: S.FactoryContactCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.CONTACT_MANAGE", plant_id=p.siteId)
    ct = FactoryContact(
        factoryProfileId=p.id, siteId=p.siteId, role=body.role, name=body.name,
        phone=body.phone, email=body.email, isPrimary=body.isPrimary, createdBy=user.id,
    )
    db.add(ct)
    await db.commit()
    await db.refresh(ct)
    return S.FactoryContactOut.model_validate(ct)


@router.patch("/contacts/{contact_id}", response_model=S.FactoryContactOut)
async def update_contact(contact_id: str, body: S.FactoryContactUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ct = await db.get(FactoryContact, contact_id)
    if not ct or ct.isDeleted:
        raise HTTPException(404, "Contact not found")
    await _require(db, user, "FACILITY.CONTACT_MANAGE", plant_id=ct.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(ct, k, v)
    ct.updatedBy = user.id
    await db.commit()
    await db.refresh(ct)
    return S.FactoryContactOut.model_validate(ct)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(contact_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ct = await db.get(FactoryContact, contact_id)
    if not ct or ct.isDeleted:
        raise HTTPException(404, "Contact not found")
    await _require(db, user, "FACILITY.CONTACT_MANAGE", plant_id=ct.siteId)
    ct.isDeleted = True
    ct.updatedBy = user.id
    await db.commit()
