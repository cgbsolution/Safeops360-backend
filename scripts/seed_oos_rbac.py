"""Additive RBAC for OOS Investigation (Pharma IMS Module 3)."""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import Permission, Role, RolePermission

ACTIONS = ["READ", "CREATE", "UPDATE", "APPROVE", "EXPORT"]
PERMISSIONS = [{"code": f"OOS.{a}", "module": "OOS", "action": a, "description": f"{a} on OOS"} for a in ACTIONS]

GRANTS: dict[str, tuple[list[str], str]] = {
    "QC_ANALYST": (["READ", "CREATE"], "OWN_PLANT"),
    "QC_SUPERVISOR": (["READ", "CREATE", "UPDATE"], "OWN_PLANT"),
    "QA_OFFICER": (["READ", "UPDATE"], "OWN_PLANT"),
    "QA_MANAGER": (["READ", "UPDATE", "APPROVE", "EXPORT"], "OWN_PLANT"),
    "QA_DIRECTOR": (ACTIONS, "OWN_PLANT"),
    "SAFETY_OFFICER": (["READ"], "OWN_PLANT"),
    "HSE_MANAGER": (ACTIONS, "OWN_PLANT"),
    "PLANT_HEAD": (ACTIONS, "OWN_PLANT"),
    "CORPORATE_HSE": (["READ", "EXPORT"], "ALL_PLANTS"),
    "ADMIN": (ACTIONS, "ALL_PLANTS"),
    "SYSTEM_ADMIN": (ACTIONS, "ALL_PLANTS"),
}


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        added_p = 0
        for p in PERMISSIONS:
            if s.execute(select(Permission).where(Permission.code == p["code"])).scalar_one_or_none() is None:
                s.add(Permission(**p)); added_p += 1
        s.flush()
        roles = {r.code: r for r in s.execute(select(Role)).scalars().all()}
        perms = {p.code: p for p in s.execute(select(Permission)).scalars().all()}
        added_g = 0
        for rc, (acts, scope) in GRANTS.items():
            role = roles.get(rc)
            if role is None:
                continue
            for a in acts:
                perm = perms.get(f"OOS.{a}")
                if perm is None:
                    continue
                ex = s.execute(select(RolePermission).where(RolePermission.roleId == role.id, RolePermission.permissionId == perm.id)).scalar_one_or_none()
                if ex is not None:
                    if ex.scope != scope:
                        ex.scope = scope
                    continue
                s.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope)); added_g += 1
        s.commit()
        print(f"OOS perms added: {added_p} | grants added: {added_g}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
