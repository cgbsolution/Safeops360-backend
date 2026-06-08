"""Additive pharma RBAC — Deviation Management (Pharma IMS Module 1).

Adds DEVIATION.* permissions + the pharma QA/QC roles + grants WITHOUT wiping
the existing RolePermission matrix (unlike app/seed/seed_rbac.py). Also grants
CAPA.{READ,CREATE,UPDATE} to QA roles so they can raise a CAPA from a deviation.
Idempotent. Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_pharma_rbac.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import Permission, Role, RolePermission

DEV_ACTIONS = ["READ", "CREATE", "UPDATE", "APPROVE", "CLOSE", "EXPORT"]

PERMISSIONS = [
    {"code": f"DEVIATION.{a}", "module": "DEVIATION", "action": a, "description": f"{a} on DEVIATION"}
    for a in DEV_ACTIONS
]

PHARMA_ROLES = [
    {"code": "QA_OFFICER", "name": "QA Officer", "description": "Creates/reviews deviations, CAPA, document review.", "sortOrder": 26, "defaultLanding": "/deviations"},
    {"code": "QA_MANAGER", "name": "QA Manager", "description": "Approves deviations + batch disposition, CAPA approval, audit lead.", "sortOrder": 12, "defaultLanding": "/deviations"},
    {"code": "QA_DIRECTOR", "name": "QA Director", "description": "Final closure for critical deviations, batch disposition sign-off.", "sortOrder": 8, "defaultLanding": "/deviations"},
    {"code": "QC_ANALYST", "name": "QC Analyst", "description": "Creates OOS / lab deviations, Phase 1 investigation.", "sortOrder": 95, "defaultLanding": "/deviations"},
    {"code": "QC_SUPERVISOR", "name": "QC Supervisor", "description": "Phase 1 supervision, Phase 2 initiation.", "sortOrder": 60, "defaultLanding": "/deviations"},
    {"code": "PRODUCTION_SUPERVISOR", "name": "Production Supervisor", "description": "Reports manufacturing deviations.", "sortOrder": 82, "defaultLanding": "/deviations"},
]

# role_code -> (DEVIATION actions, scope)
DEV_GRANTS: dict[str, tuple[list[str], str]] = {
    "QA_OFFICER": (["READ", "CREATE", "UPDATE"], "OWN_PLANT"),
    "QA_MANAGER": (["READ", "CREATE", "UPDATE", "APPROVE", "EXPORT"], "OWN_PLANT"),
    "QA_DIRECTOR": (["READ", "CREATE", "UPDATE", "APPROVE", "CLOSE", "EXPORT"], "OWN_PLANT"),
    "QC_ANALYST": (["READ", "CREATE"], "OWN_PLANT"),
    "QC_SUPERVISOR": (["READ", "CREATE", "UPDATE"], "OWN_PLANT"),
    "PRODUCTION_SUPERVISOR": (["READ", "CREATE"], "OWN_PLANT"),
    # Existing roles so current demo users can drive the workflow:
    "SAFETY_OFFICER": (["READ", "CREATE", "UPDATE"], "OWN_PLANT"),
    "HSE_MANAGER": (["READ", "CREATE", "UPDATE", "APPROVE", "CLOSE", "EXPORT"], "OWN_PLANT"),
    "PLANT_HEAD": (["READ", "CREATE", "UPDATE", "APPROVE", "CLOSE", "EXPORT"], "OWN_PLANT"),
    "CORPORATE_HSE": (["READ", "EXPORT"], "ALL_PLANTS"),
    "ADMIN": (DEV_ACTIONS, "ALL_PLANTS"),
    "SYSTEM_ADMIN": (DEV_ACTIONS, "ALL_PLANTS"),
}

# Roles that can raise a CAPA from a deviation (need CAPA.CREATE). Only applied
# if the CAPA.* permission rows already exist (seeded canonically elsewhere).
CAPA_GRANT_ROLES = ["QA_OFFICER", "QA_MANAGER", "QA_DIRECTOR", "QC_SUPERVISOR"]
CAPA_ACTIONS = ["READ", "CREATE", "UPDATE"]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        perms_added = 0
        for p in PERMISSIONS:
            if s.execute(select(Permission).where(Permission.code == p["code"])).scalar_one_or_none() is None:
                s.add(Permission(**p))
                perms_added += 1
        s.flush()

        roles_added = 0
        for r in PHARMA_ROLES:
            if s.execute(select(Role).where(Role.code == r["code"])).scalar_one_or_none() is None:
                s.add(Role(code=r["code"], name=r["name"], description=r["description"],
                           isSystem=False, sortOrder=r["sortOrder"], defaultLanding=r["defaultLanding"], isActive=True))
                roles_added += 1
        s.flush()

        roles_by_code = {r.code: r for r in s.execute(select(Role)).scalars().all()}
        perms_by_code = {p.code: p for p in s.execute(select(Permission)).scalars().all()}

        def grant(role_code: str, codes: list[str], scope: str) -> int:
            role = roles_by_code.get(role_code)
            if role is None:
                return 0
            n = 0
            for code in codes:
                perm = perms_by_code.get(code)
                if perm is None:
                    continue
                existing = s.execute(
                    select(RolePermission).where(
                        RolePermission.roleId == role.id, RolePermission.permissionId == perm.id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.scope != scope:
                        existing.scope = scope
                    continue
                s.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                n += 1
            return n

        grants_added = 0
        for role_code, (actions, scope) in DEV_GRANTS.items():
            grants_added += grant(role_code, [f"DEVIATION.{a}" for a in actions], scope)
        capa_added = 0
        for role_code in CAPA_GRANT_ROLES:
            capa_added += grant(role_code, [f"CAPA.{a}" for a in CAPA_ACTIONS], "OWN_PLANT")

        s.commit()
        print(f"Permissions added : {perms_added} (DEVIATION catalogue {len(PERMISSIONS)})")
        print(f"Roles added       : {roles_added} ({', '.join(r['code'] for r in PHARMA_ROLES)})")
        print(f"DEVIATION grants  : {grants_added}")
        print(f"CAPA grants to QA : {capa_added} (0 if CAPA.* not yet seeded)")
        print("Done. Pharma RBAC applied additively.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
