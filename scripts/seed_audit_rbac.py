"""Additive RBAC for Audit Management (Pharma IMS Module 4)."""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import Permission, Role, RolePermission

ACTIONS = ["READ", "CREATE", "UPDATE", "APPROVE", "CLOSE", "EXPORT"]
PERMISSIONS = [{"code": f"AUDIT.{a}", "module": "AUDIT", "action": a, "description": f"{a} on AUDIT (audit management)"} for a in ACTIONS]

ROLE_DEF = {"code": "AUDITOR", "name": "GMP Auditor", "description": "Plans and conducts internal/supplier GMP audits; raises findings.",
            "sortOrder": 50, "defaultLanding": "/audits"}

GRANTS: dict[str, tuple[list[str], str]] = {
    "AUDITOR": (ACTIONS, "OWN_PLANT"),
    "QA_OFFICER": (["READ", "CREATE", "UPDATE"], "OWN_PLANT"),
    "QA_MANAGER": (ACTIONS, "OWN_PLANT"),
    "QA_DIRECTOR": (ACTIONS, "OWN_PLANT"),
    "SAFETY_OFFICER": (["READ", "CREATE", "UPDATE"], "OWN_PLANT"),
    "HSE_MANAGER": (ACTIONS, "OWN_PLANT"),
    "PLANT_HEAD": (ACTIONS, "OWN_PLANT"),
    "CORPORATE_HSE": (["READ", "EXPORT"], "ALL_PLANTS"),
    "ADMIN": (ACTIONS, "ALL_PLANTS"),
    "SYSTEM_ADMIN": (ACTIONS, "ALL_PLANTS"),
}
# Roles that raise CAPA from findings need CAPA.CREATE (if CAPA.* is seeded).
CAPA_ROLES = ["AUDITOR", "QA_OFFICER", "QA_MANAGER", "QA_DIRECTOR"]
CAPA_ACTIONS = ["READ", "CREATE", "UPDATE"]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        added_p = 0
        for p in PERMISSIONS:
            if s.execute(select(Permission).where(Permission.code == p["code"])).scalar_one_or_none() is None:
                s.add(Permission(**p)); added_p += 1
        if s.execute(select(Role).where(Role.code == ROLE_DEF["code"])).scalar_one_or_none() is None:
            s.add(Role(code=ROLE_DEF["code"], name=ROLE_DEF["name"], description=ROLE_DEF["description"],
                       isSystem=False, sortOrder=ROLE_DEF["sortOrder"], defaultLanding=ROLE_DEF["defaultLanding"], isActive=True))
        s.flush()
        roles = {r.code: r for r in s.execute(select(Role)).scalars().all()}
        perms = {p.code: p for p in s.execute(select(Permission)).scalars().all()}

        def grant(rc, codes, scope):
            role = roles.get(rc)
            if role is None:
                return 0
            n = 0
            for code in codes:
                perm = perms.get(code)
                if perm is None:
                    continue
                ex = s.execute(select(RolePermission).where(RolePermission.roleId == role.id, RolePermission.permissionId == perm.id)).scalar_one_or_none()
                if ex is not None:
                    if ex.scope != scope:
                        ex.scope = scope
                    continue
                s.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope)); n += 1
            return n

        added_g = 0
        for rc, (acts, scope) in GRANTS.items():
            added_g += grant(rc, [f"AUDIT.{a}" for a in acts], scope)
        capa_g = 0
        for rc in CAPA_ROLES:
            capa_g += grant(rc, [f"CAPA.{a}" for a in CAPA_ACTIONS], "OWN_PLANT")
        s.commit()
        print(f"AUDIT perms added: {added_p} | grants added: {added_g} | CAPA grants: {capa_g} | AUDITOR role ensured")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
