"""Verify Audit Management E2E (Pharma IMS Module 4).

create -> start -> add finding -> issue report (wrong pw rejected, right pw
signs) -> respond -> raise CAPA (real, via a CAPA-permitted user) -> close
finding -> close audit (signs). Asserts statuses, the real finding->CAPA link,
and signature validity. Throwaway rows + temp signer hard-deleted.
"""

from __future__ import annotations

import asyncio

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.audit_mgmt import Audit, AuditFinding  # noqa: E402
from app.models.capa import Capa  # noqa: E402
from app.models.part11 import ElectronicSignature, GmpAuditEntry  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import audit_mgmt as svc  # noqa: E402
from app.services import part11  # noqa: E402

TEMP_EMAIL = "audit.verify@local.test"
TEMP_PW = "Aud@1234"


def ok(c, label):
    print(f"  [{'PASS' if c else 'FAIL'}] {label}")
    if not c:
        raise SystemExit(1)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        lms = (await db.execute(select(Plant).where(Plant.code == "LMS"))).scalar_one_or_none() or (await db.execute(select(Plant))).scalars().first()
        users = (await db.execute(select(User).where(User.plantId == lms.id))).scalars().all()
        capa_user = next((u for u in users if (u.role or "").upper() in ("ADMIN", "SYSTEM_ADMIN")), None) or next((u for u in users if "admin" in u.email.lower()), None)

        await db.execute(delete(User).where(User.email == TEMP_EMAIL))
        await db.flush()
        signer = User(email=TEMP_EMAIL, name="Audit Verify Lead", passwordHash=hash_password(TEMP_PW), role="QA_MANAGER", plantId=lms.id, department="QA")
        db.add(signer)
        await db.flush()
        print(f"Plant {lms.code} | signer {signer.name} | capa_user {(capa_user.name if capa_user else 'none')}")

        print("\nE2E:")
        a = await svc.create_audit(db, user=signer, data={"plantId": lms.id, "title": "E2E verify internal audit", "auditType": "internal_gmp"})
        await db.flush()
        ok(a.status == "planned", f"created -> planned ({a.number})")

        await svc.start_audit(db, a=a, user=signer)
        await db.flush()
        ok(a.status == "in_progress", "started -> in_progress")

        f = await svc.add_finding(db, a=a, user=signer, type_="major", description="Cleaning log entry missing for shift.", area="Compression", reference_requirement="EU GMP Ch.4")
        await db.flush()
        ok(f.findingStatus == "open" and f.findingNumber.endswith("-F01"), f"finding added ({f.findingNumber})")

        bad = False
        try:
            await svc.issue_report(db, a=a, user=signer, password="WRONG")
        except part11.SignatureError:
            bad = True
        ok(bad, "issue report WRONG password -> rejected")

        await svc.issue_report(db, a=a, user=signer, password=TEMP_PW)
        await db.flush()
        ok(a.status == "response_pending", "report issued (signed) -> response_pending")

        await svc.respond_finding(db, finding=f, audit=a, user=signer, response="Accepted; CAPA to be raised.")
        await db.flush()

        if capa_user is not None:
            await svc.raise_capa_for_finding(db, audit=a, finding=f, user=capa_user, owner_user_id=signer.id)
            await db.flush()
            ok(f.capaId is not None and a.status == "capa_monitoring", f"finding raised real CAPA {f.capaNumber}, audit -> capa_monitoring")
            capa = await db.get(Capa, f.capaId)
            ok(capa is not None and capa.sourceReferenceId == f.id, "linked CAPA is a real row, back-linked to the finding")
        else:
            print("  [SKIP] raise CAPA (no CAPA-permitted user found)")

        await svc.close_finding(db, finding=f, audit=a, user=signer)
        await db.flush()
        ok(f.findingStatus == "closed", "finding closed")

        await svc.close_audit(db, a=a, user=signer, password=TEMP_PW)
        await db.flush()
        ok(a.status == "closed", "audit closed (signed)")

        sigs = await part11.signatures_for(db, "audit", a.id, current_snapshot=svc.audit_snapshot(a))
        ok(len(sigs) == 2 and all(x["isValid"] for x in sigs), "2 valid signatures (report issued + closure)")

        # cleanup
        if f.capaId:
            await db.execute(delete(Capa).where(Capa.id == f.capaId))
        await db.execute(delete(GmpAuditEntry).where(GmpAuditEntry.recordId == a.id))
        await db.execute(delete(ElectronicSignature).where(ElectronicSignature.recordId == a.id))
        await db.execute(delete(AuditFinding).where(AuditFinding.auditId == a.id))
        await db.execute(delete(Audit).where(Audit.id == a.id))
        await db.execute(delete(User).where(User.id == signer.id))
        await db.commit()
        print("  cleaned up throwaway audit + CAPA + temp signer")

    print("\nAll Audit Management checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
