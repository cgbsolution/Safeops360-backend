"""Seed the PPE Management module (PPE-01).

Two parts, both idempotent:
  1. Global PPE Type library (~41 types across every category) — upserted by
     code, so re-running is safe and additive.
  2. Demo inventory for the LMS plant — items across the lifecycle (in stock,
     issued, overdue inspection, approaching end-of-life, quarantined, retired),
     issuances to real users, a few inspection records, and role-scoped
     requirement profiles so the People Compliance view has a realistic mix of
     compliant / gaps / critical. Skipped if the plant already has PPE items.

Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_ppe.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.plant import Plant
from app.models.ppe import (
    PpeInspection,
    PpeIssuance,
    PpeItem,
    PpeRequirementProfile,
    PpeType,
)
from app.models.user import User
from app.services.ppe_inventory import ALL_ROLES, add_years

NOW = datetime.now(timezone.utc)


def _std(s: str) -> list[dict]:
    return [{"standard": s, "clause": "Full standard", "requirement": ""}] if s else []


def _schedule(periodic_days: int | None, third_party: bool, annual: bool = False) -> list[dict]:
    sched = [{"inspection_type": "pre_use", "interval_days": None, "requires_competent_person": False,
              "requires_third_party": False, "regulatory_reference": ""}]
    if periodic_days:
        sched.append({
            "inspection_type": "annual" if annual else "periodic",
            "interval_days": periodic_days,
            "requires_competent_person": True,
            "requires_third_party": third_party,
            "regulatory_reference": "",
        })
    return sched


# (code, name, category, subcategory, life_yrs, tracks, personal, competency, fit_mo,
#  permit_types, standard, periodic_days, third_party)
CATALOG: list[tuple] = [
    ("HELMET-IS3521", "Industrial Safety Helmet", "head_protection", "safety_helmet", 5, True, False, None, None, ["confined_space", "work_at_height", "hot_work", "general_cold", "electrical"], "IS 3521", 180, False),
    ("HELMET-VENTILATED", "Vented Safety Helmet", "head_protection", "safety_helmet", 5, True, False, None, None, ["general_cold"], "EN 397", 180, False),
    ("HARNESS-FULLBODY-EN361", "Full-Body Safety Harness", "fall_protection", "full_body_harness", 10, True, True, "WORK-HEIGHT-L1", None, ["work_at_height"], "EN 361", 365, True),
    ("LANYARD-SHOCKABSORB", "Shock-Absorbing Lanyard", "fall_protection", "lanyard", 10, True, True, None, None, ["work_at_height"], "EN 355", 365, False),
    ("LANYARD-TWINTAIL", "Twin-Tail Lanyard", "fall_protection", "lanyard", 10, True, True, None, None, ["work_at_height"], "EN 355", 365, False),
    ("SRL-FALLARREST", "Self-Retracting Lifeline", "fall_protection", "srl", 10, True, False, None, None, ["work_at_height"], "EN 360", 365, True),
    ("RESCUE-TRIPOD", "Confined Space Rescue Tripod", "fall_protection", "rescue_tripod", 10, True, False, None, None, ["confined_space"], "EN 795", 365, True),
    ("SCBA-POSITIVEPRESSURE", "Self-Contained Breathing Apparatus (SCBA)", "respiratory_protection", "scba", 15, True, False, "CS-ENTRANT-L1", None, ["confined_space"], "EN 137", 365, True),
    ("AIRLINE-RESP", "Airline Breathing Apparatus", "respiratory_protection", "airline", 15, True, False, "CS-ENTRANT-L1", None, ["confined_space"], "EN 14593", 365, True),
    ("RESP-HALFMASK", "Half-Face Respirator", "respiratory_protection", "half_mask", 5, True, True, None, 12, [], "EN 140", 180, False),
    ("RESP-FULLFACE", "Full-Face Respirator", "respiratory_protection", "full_mask", 8, True, True, None, 12, ["confined_space"], "EN 136", 180, False),
    ("FFP3-MASK", "FFP3 Filtering Half Mask", "respiratory_protection", "disposable", 0, False, False, None, None, [], "EN 149", None, False),
    ("DUST-MASK-FFP2", "FFP2 Dust Mask", "respiratory_protection", "disposable", 0, False, False, None, None, [], "EN 149", None, False),
    ("GAS-DETECTOR-4GAS", "Portable 4-Gas Detector (O2, LEL, H2S, CO)", "gas_detection", "gas_monitor_4gas", 5, True, False, "GAS-TEST", None, ["confined_space"], "EN 60079", 365, False),
    ("GAS-DETECTOR-SINGLE-H2S", "Single-Gas H2S Detector", "gas_detection", "gas_monitor_single", 3, True, False, "GAS-TEST", None, ["confined_space"], "EN 60079", 365, False),
    ("GOGGLES-CHEM", "Chemical Splash Goggles", "eye_face_protection", "goggles", 3, True, False, None, None, [], "EN 166", None, False),
    ("SPECTACLES-SAFETY", "Safety Spectacles", "eye_face_protection", "spectacles", 3, True, False, None, None, ["general_cold"], "EN 166", None, False),
    ("VISOR-WELDING-AUTO", "Auto-Darkening Welding Visor", "eye_face_protection", "welding_visor", 5, True, True, None, None, ["hot_work"], "EN 175", None, False),
    ("FACESHIELD-GRINDING", "Grinding Face Shield", "eye_face_protection", "face_shield", 3, True, False, None, None, ["hot_work"], "EN 166", None, False),
    ("EARMUFF", "Ear Defenders (Muffs)", "hearing_protection", "earmuff", 5, True, True, None, None, [], "EN 352", None, False),
    ("EARPLUG-REUSABLE", "Reusable Ear Plugs", "hearing_protection", "earplug", 1, False, True, None, None, [], "EN 352", None, False),
    ("GLOVES-GENERAL", "General Handling Gloves", "hand_protection", "general", 1, False, True, None, None, ["general_cold"], "EN 388", None, False),
    ("GLOVES-CUT5", "Cut-Resistant Gloves (Level 5)", "hand_protection", "cut_resistant", 2, True, True, None, None, [], "EN 388", None, False),
    ("GLOVES-CHEM-NITRILE", "Chemical Nitrile Gloves", "hand_protection", "chemical", 2, True, True, None, None, [], "EN 374", None, False),
    ("GLOVES-HEAT", "Heat-Resistant Gloves", "hand_protection", "heat_resistant", 2, True, True, None, None, ["hot_work"], "EN 407", None, False),
    ("WELDING-GAUNTLET", "Welding Gauntlets", "hand_protection", "welding", 2, True, True, None, None, ["hot_work"], "EN 12477", None, False),
    ("GLOVES-ELEC-LV", "Electrical Insulating Gloves — Low Voltage", "electrical_protection", "elec_glove_lv", 3, True, True, None, None, ["electrical"], "IS 4770", 180, True),
    ("GLOVES-ELEC-HT", "Electrical Insulating Gloves — High Tension", "electrical_protection", "elec_glove_ht", 3, True, True, None, None, ["electrical_ht"], "IS 4770", 90, True),
    ("ARC-FLASH-SUIT", "Arc Flash Suit", "electrical_protection", "arc_flash", 5, True, True, None, None, ["electrical_ht"], "IEC 61482", 365, False),
    ("SHOES-SAFETY", "Safety Shoes (Steel Toe)", "foot_protection", "safety_shoes", 2, True, True, None, None, ["confined_space", "work_at_height", "hot_work", "general_cold"], "EN ISO 20345", None, False),
    ("GUMBOOTS-SAFETY", "Safety Gumboots", "foot_protection", "gumboots", 2, True, True, None, None, [], "EN ISO 20345", None, False),
    ("SHOES-ELEC", "Electrical Hazard Footwear", "foot_protection", "elec_footwear", 2, True, True, None, None, ["electrical"], "EN ISO 20345", None, False),
    ("COVERALL-COTTON", "Cotton Coverall", "body_protection", "coverall", 2, True, True, None, None, ["general_cold"], "", None, False),
    ("COVERALL-FR", "Flame-Resistant Coverall", "body_protection", "fr_coverall", 3, True, True, None, None, ["hot_work"], "EN ISO 11612", None, False),
    ("APRON-WELDING-LEATHER", "Leather Welding Apron", "body_protection", "welding_apron", 3, True, True, None, None, ["hot_work"], "EN ISO 11611", None, False),
    ("SUIT-CHEM-TYPE3", "Chemical Protective Suit (Type 3)", "chemical_protection", "chem_suit", 3, True, False, "CHEMICAL-HANDLING", None, [], "EN 14605", None, False),
    ("APRON-CHEM-PVC", "PVC Chemical Apron", "chemical_protection", "chem_apron", 2, True, True, None, None, [], "EN 14605", None, False),
    ("VEST-HIVIS", "High-Visibility Vest", "visibility_high", "hi_vis_vest", 2, True, True, None, None, ["excavation", "general_cold"], "EN ISO 20471", None, False),
    ("RAINCOAT-HIVIS", "Hi-Vis Rain Suit", "visibility_high", "hi_vis_rain", 2, True, True, None, None, [], "EN ISO 20471", None, False),
    ("LIFEJACKET", "Life Jacket / PFD", "body_protection", "life_jacket", 5, True, False, None, None, [], "EN ISO 12402", 365, False),
    ("KNEE-PADS", "Knee Protection Pads", "body_protection", "knee_pads", 2, True, True, None, None, [], "EN 14404", None, False),
]


def seed_catalog(s: Session) -> int:
    existing = set(s.execute(select(PpeType.code)).scalars().all())
    added = 0
    for (code, name, category, subcat, life, tracks, personal, comp, fit_mo,
         permits, standard, periodic, third_party) in CATALOG:
        if code in existing:
            continue
        s.add(PpeType(
            tenantId=None,
            code=code,
            name=name,
            description=f"{name} — {category.replace('_', ' ')}.",
            category=category,
            subcategory=subcat,
            applicableStandards=_std(standard),
            minimumSpecification=f"Conforms to {standard}." if standard else "",
            controlsHazards=[],
            enablesPermitTypes=permits,
            requiredForAreas=[],
            serviceLifeYears=life,
            inspectionSchedule=_schedule(periodic, third_party, annual=(periodic == 365)),
            requiresCompetencyToUse=comp,
            requiresFitTest=fit_mo is not None,
            fitTestValidityMonths=fit_mo,
            requiredTrainingPrograms=[],
            tracksIndividualItems=tracks,
            reorderPointPer100Workers=10,
            isPersonalIssue=personal,
            statutoryProvisionRequired=True,
            regulatoryReferences=[{"regulation": "Factories Act 1948", "section": "Section 35", "requirement": "Employer must provide and maintain PPE"}],
            isActive=True,
            isGlobal=True,
        ))
        added += 1
    s.flush()
    return added


def _resolve_plant(s: Session) -> Plant:
    plant = s.execute(select(Plant).where(Plant.code == "LMS")).scalar_one_or_none()
    if plant is None:
        plant = s.execute(select(Plant).where(Plant.name.ilike("%Lumshnong%"))).scalars().first()
    if plant is None:
        plant = s.execute(select(Plant)).scalars().first()
    return plant


_seq = {"item": {}, "iss": 0}


def _commission(s: Session, plant: Plant, t: PpeType, qty: int, *, mfg_age_days: int,
                next_due_days: int | None, status: str = "in_stock") -> list[PpeItem]:
    sub = (t.subcategory or t.code.split("-")[0]).upper().replace("_", "")[:10]
    out: list[PpeItem] = []
    mfg = NOW - timedelta(days=mfg_age_days)
    for _ in range(qty):
        n = _seq["item"].get(t.code, 0) + 1
        _seq["item"][t.code] = n
        item = PpeItem(
            tenantId=None,
            itemNumber=f"PPE-{plant.code}-{sub}-{n:04d}",
            serialNumber=f"{t.code}-{NOW.year}-{n:04d}",
            ppeTypeId=t.id,
            ppeTypeCode=t.code,
            ppeTypeName=t.name,
            manufacturer="3M / Honeywell / Karam",
            model=f"{t.subcategory or t.code}-STD",
            batchLotNumber=f"LOT-{NOW.year}-{(n % 4) + 1}",
            manufactureDate=mfg,
            purchaseDate=mfg + timedelta(days=20),
            cost=None,
            plantId=plant.id,
            storageLocation=f"Safety Store — Rack {sub[:2]}",
            status=status,
            condition="new" if status == "in_stock" else "good",
            commissionedAt=mfg + timedelta(days=22),
            serviceLifeEndDate=add_years(mfg, t.serviceLifeYears),
            nextInspectionDueDate=(NOW + timedelta(days=next_due_days)) if next_due_days is not None else None,
            stateHistory=[{
                "from_status": "—", "to_status": status,
                "changed_at": (mfg + timedelta(days=22)).isoformat(),
                "changed_by_user_id": "SEED", "reason": "Commissioned (goods receipt)",
            }],
            versionNumber=1,
        )
        s.add(item)
        out.append(item)
    s.flush()
    return out


def _issue(s: Session, plant: Plant, item: PpeItem, user: User, issuer: User, purpose: str = "personal_assignment") -> PpeIssuance:
    _seq["iss"] += 1
    iss = PpeIssuance(
        tenantId=None,
        issuanceNumber=f"ISS-{plant.code}-{NOW.year}-{_seq['iss']:04d}",
        ppeItemId=item.id,
        ppeTypeCode=item.ppeTypeCode,
        ppeTypeName=item.ppeTypeName,
        serialNumber=item.serialNumber,
        issuedToUserId=user.id,
        issuedToName=user.name,
        issuedToDepartment=user.department or "",
        issuedToRole=user.role or "",
        issuedByUserId=issuer.id,
        issuedByName=issuer.name,
        issuedAt=NOW - timedelta(days=30),
        issuancePurpose=purpose,
        conditionAtIssuance="good",
        preIssuanceInspectionDone=True,
        preIssuanceInspectorUserId=issuer.id,
        recipientAcknowledged=True,
        recipientAcknowledgedAt=NOW - timedelta(days=30),
        briefingProvided=True,
        briefingByUserId=issuer.id,
        status="active",
        plantId=plant.id,
    )
    s.add(iss)
    s.flush()
    item.status = "issued"
    item.currentHolderUserId = user.id
    item.currentIssuanceId = iss.id
    item.issuedSince = NOW - timedelta(days=30)
    item.condition = "good"
    item.stateHistory = list(item.stateHistory) + [{
        "from_status": "in_stock", "to_status": "issued",
        "changed_at": (NOW - timedelta(days=30)).isoformat(),
        "changed_by_user_id": issuer.id, "reason": f"Issued to {user.name} ({purpose})",
    }]
    return iss


def _profile(s: Session, plant: Plant, scope_id: str, scope_name: str, required: list[dict]) -> None:
    exists = s.execute(
        select(PpeRequirementProfile)
        .where(PpeRequirementProfile.plantId == plant.id)
        .where(PpeRequirementProfile.scopeType == "role")
        .where(PpeRequirementProfile.scopeId == scope_id)
    ).scalar_one_or_none()
    if exists is not None:
        return
    s.add(PpeRequirementProfile(
        tenantId=None, plantId=plant.id, scopeType="role", scopeId=scope_id,
        scopeName=scope_name, requiredPpe=required, isActive=True,
    ))


def _req(types: dict[str, PpeType], code: str, level: str, rationale: str = "") -> dict:
    t = types[code]
    return {
        "ppe_type_id": t.id, "ppe_type_code": t.code, "ppe_type_name": t.name,
        "requirement_level": level, "condition": None, "minimum_specification": "",
        "substitution_allowed": [], "rationale": rationale,
        "regulatory_reference": "Factories Act 1948 s.35",
    }


def _inspection(s: Session, item: PpeItem, *, conducted_days_ago: int, result: str, inspector: User,
                inspection_type: str = "annual") -> None:
    s.add(PpeInspection(
        tenantId=None, ppeItemId=item.id, ppeTypeCode=item.ppeTypeCode, serialNumber=item.serialNumber,
        inspectionType=inspection_type, trigger="scheduled", conductedAt=NOW - timedelta(days=conducted_days_ago),
        inspectorUserId=inspector.id, inspectorName=inspector.name, inspectorQualification="Competent Person",
        checklistItems=[
            {"sequence": 1, "check_item": "Webbing / shell integrity", "result": "pass", "notes": "", "photo_evidence_url": None},
            {"sequence": 2, "check_item": "Stitching / buckles / D-rings", "result": result, "notes": "", "photo_evidence_url": None},
        ],
        overallResult=result,
        defectsFound=[] if result == "pass" else [{"defect_description": "Minor wear noted", "severity": "minor", "action_required": "Monitor at next inspection"}],
        itemStatusAfterInspection="returned_to_service" if result != "fail" else "quarantined_pending_repair",
        plantId=item.plantId,
    ))


def seed_demo(s: Session, plant: Plant, types: dict[str, PpeType]) -> dict:
    users = s.execute(select(User).where(User.plantId == plant.id)).scalars().all()
    issuer = next((u for u in users if u.role in ("STORE_KEEPER", "SAFETY_OFFICER", "HSE_MANAGER", "ADMIN", "SYSTEM_ADMIN")), users[0] if users else None)

    # ── Commission stock, spread across inspection-due buckets + lifecycle ──
    helmets = (
        _commission(s, plant, types["HELMET-IS3521"], 3, mfg_age_days=400, next_due_days=-12)   # overdue
        + _commission(s, plant, types["HELMET-IS3521"], 4, mfg_age_days=400, next_due_days=5)    # this week
        + _commission(s, plant, types["HELMET-IS3521"], 5, mfg_age_days=400, next_due_days=22)   # this month
        + _commission(s, plant, types["HELMET-IS3521"], 30, mfg_age_days=400, next_due_days=210)  # current
    )
    shoes = _commission(s, plant, types["SHOES-SAFETY"], 42, mfg_age_days=300, next_due_days=None)
    harness_overdue = _commission(s, plant, types["HARNESS-FULLBODY-EN361"], 2, mfg_age_days=800, next_due_days=-18)
    harness_eol = _commission(s, plant, types["HARNESS-FULLBODY-EN361"], 2, mfg_age_days=3590, next_due_days=200)  # ~9.8y → EOL soon
    harness_current = _commission(s, plant, types["HARNESS-FULLBODY-EN361"], 10, mfg_age_days=800, next_due_days=240)
    harness_duesoon = _commission(s, plant, types["HARNESS-FULLBODY-EN361"], 3, mfg_age_days=800, next_due_days=20)  # inspection due soon
    scba = _commission(s, plant, types["SCBA-POSITIVEPRESSURE"], 5, mfg_age_days=500, next_due_days=250)
    gas = _commission(s, plant, types["GAS-DETECTOR-4GAS"], 1, mfg_age_days=500, next_due_days=-6) \
        + _commission(s, plant, types["GAS-DETECTOR-4GAS"], 5, mfg_age_days=500, next_due_days=250)
    elec = _commission(s, plant, types["GLOVES-ELEC-LV"], 8, mfg_age_days=300, next_due_days=60)
    _commission(s, plant, types["VISOR-WELDING-AUTO"], 5, mfg_age_days=300, next_due_days=None)
    _commission(s, plant, types["COVERALL-FR"], 10, mfg_age_days=200, next_due_days=None)
    _commission(s, plant, types["VEST-HIVIS"], 20, mfg_age_days=200, next_due_days=None)
    _commission(s, plant, types["EARMUFF"], 15, mfg_age_days=200, next_due_days=None)
    _commission(s, plant, types["LANYARD-SHOCKABSORB"], 1, mfg_age_days=800, next_due_days=-9) \
        + _commission(s, plant, types["LANYARD-SHOCKABSORB"], 8, mfg_age_days=800, next_due_days=240)

    # Lifecycle variety: 1 retired helmet, 1 quarantined harness.
    helmets[-1].status = "retired"
    helmets[-1].condition = "unserviceable"
    helmets[-1].stateHistory = list(helmets[-1].stateHistory) + [{
        "from_status": "in_stock", "to_status": "retired", "changed_at": NOW.isoformat(),
        "changed_by_user_id": "SEED", "reason": "End-of-life disposal (demo)"}]
    harness_eol[-1].status = "quarantined"
    harness_eol[-1].condition = "unserviceable"

    # ── Role requirement profiles ──
    _profile(s, plant, ALL_ROLES, "All site personnel", [
        _req(types, "HELMET-IS3521", "mandatory", "Head protection mandated site-wide"),
        _req(types, "SHOES-SAFETY", "mandatory", "Foot protection mandated site-wide"),
        _req(types, "GLOVES-GENERAL", "recommended", "General hand protection"),
    ])
    _profile(s, plant, "WORKER", "Worker", [
        _req(types, "HARNESS-FULLBODY-EN361", "mandatory", "Routine work at height"),
        _req(types, "VEST-HIVIS", "recommended", "Visibility in vehicle-movement areas"),
    ])
    _profile(s, plant, "CONTRACTOR_WORKMAN", "Contractor Workman", [
        _req(types, "HARNESS-FULLBODY-EN361", "mandatory", "Routine work at height"),
        _req(types, "VEST-HIVIS", "mandatory", "Contractor visibility requirement"),
    ])
    _profile(s, plant, "SAFETY_OFFICER", "Safety Officer", [
        _req(types, "GAS-DETECTOR-4GAS", "mandatory", "Atmosphere monitoring during entries"),
    ])
    _profile(s, plant, "SUPERVISOR", "Supervisor", [
        _req(types, "VEST-HIVIS", "recommended", "Supervisory presence in active areas"),
    ])

    # ── Issue PPE (creates the compliant / gap / critical mix) ──
    # Everyone gets base helmet + shoes. Workers get a harness from one of three
    # pools so the People Compliance view shows all three states:
    #   current  -> compliant (green)   warn -> gaps (amber)   overdue/none -> critical (red)
    harness_warn = harness_duesoon + [h for h in harness_eol if h.status == "in_stock"]
    issuances = 0
    worker_idx = 0
    if issuer and users:
        for u in users:
            if helmets:
                h = next((x for x in helmets if x.status == "in_stock"), None)
                if h:
                    _issue(s, plant, h, u, issuer)
                    issuances += 1
            if shoes:
                sh = next((x for x in shoes if x.status == "in_stock"), None)
                if sh:
                    _issue(s, plant, sh, u, issuer)
                    issuances += 1
            role = (u.role or "").upper()
            if role in ("WORKER", "CONTRACTOR_WORKMAN"):
                worker_idx += 1
                if worker_idx % 9 == 0 and harness_overdue:
                    _issue(s, plant, harness_overdue.pop(), u, issuer)  # held but inspection overdue → critical
                    issuances += 1
                elif worker_idx % 5 == 0 and harness_warn:
                    _issue(s, plant, harness_warn.pop(), u, issuer)  # held but due-soon / EOL → gaps
                    issuances += 1
                elif worker_idx % 4 != 0 and harness_current:
                    _issue(s, plant, harness_current.pop(), u, issuer)  # compliant
                    issuances += 1
                # else: no harness → critical gap (harness mandatory for workers)
            if role == "SAFETY_OFFICER" and gas:
                g = next((x for x in gas if x.status == "in_stock"), None)
                if g:
                    _issue(s, plant, g, u, issuer)
                    issuances += 1

    # ── A couple of inspection records for item history ──
    if issuer:
        for it in harness_current[:2]:
            _inspection(s, it, conducted_days_ago=120, result="pass", inspector=issuer)
        for it in harness_overdue:  # the still-in-stock overdue ones (not issued)
            if it.status == "in_stock":
                _inspection(s, it, conducted_days_ago=400, result="conditional_pass", inspector=issuer)

    s.flush()
    return {"users": len(users), "issuances": issuances}


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as s:
        added = seed_catalog(s)
        types = {t.code: t for t in s.execute(select(PpeType)).scalars().all()}
        plant = _resolve_plant(s)
        if plant is None:
            print("!! No plant found — cannot seed demo inventory.")
            s.commit()
            print(f"Catalog: +{added} types (total {len(types)}).")
            return 1

        if "--reset" in sys.argv:
            item_ids = s.execute(select(PpeItem.id).where(PpeItem.plantId == plant.id)).scalars().all()
            if item_ids:
                s.execute(delete(PpeInspection).where(PpeInspection.ppeItemId.in_(item_ids)))
                s.execute(delete(PpeIssuance).where(PpeIssuance.plantId == plant.id))
                s.execute(delete(PpeItem).where(PpeItem.plantId == plant.id))
            s.execute(delete(PpeRequirementProfile).where(PpeRequirementProfile.plantId == plant.id))
            s.flush()
            print(f"   --reset: cleared {len(item_ids)} items + issuances + profiles for {plant.code}")

        existing_items = s.execute(select(func.count(PpeItem.id)).where(PpeItem.plantId == plant.id)).scalar_one()
        demo = {"users": 0, "issuances": 0, "skipped": True}
        if existing_items == 0:
            demo = seed_demo(s, plant, types)
            demo["skipped"] = False

        s.commit()

        total_items = s.execute(select(func.count(PpeItem.id)).where(PpeItem.plantId == plant.id)).scalar_one()
        print(f"Plant            : {plant.code}  {plant.name}  ({plant.id})")
        print(f"Catalog          : +{added} new types (total {len(types)})")
        if demo.get("skipped"):
            print(f"Demo inventory   : skipped — plant already has {existing_items} PPE items")
        else:
            print(f"Demo inventory   : {total_items} items, {demo['issuances']} issuances to {demo['users']} users")
        print("Done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
