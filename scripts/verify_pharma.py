"""Verify the Deviation + Part 11 services against live data (Pharma IMS).

1. trending() returns a sane split.
2. Full workflow E2E through the SERVICE layer on a throwaway deviation:
   create -> classify -> investigate -> disposition (WRONG pw rejected, CORRECT pw
   signs) -> close (signs). Then proves 21 CFR Part 11 properties:
     • re-authentication blocks a bad password,
     • a signature is valid while the record is unchanged,
     • editing the signed record INVALIDATES the signature (tamper-evident),
     • every GMP audit entry verifies intact, and tampering one breaks its hash.
3. Confirms a seeded closed_with_capa deviation has a REAL linked Capa row.
Throwaway rows + the temp signer are hard-deleted at the end.

Run from the backend root:
    .venv/Scripts/python.exe scripts/verify_pharma.py
"""

from __future__ import annotations

import asyncio

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.capa import Capa  # noqa: E402
from app.models.deviation import Deviation  # noqa: E402
from app.models.part11 import ElectronicSignature, GmpAuditEntry  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import deviation as dsvc  # noqa: E402
from app.services import part11  # noqa: E402

TEMP_EMAIL = "esign.verify@local.test"
TEMP_PW = "Sign@1234"


def ok(cond: bool, label: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        lms = (await db.execute(select(Plant).where(Plant.code == "LMS"))).scalar_one_or_none() or (await db.execute(select(Plant))).scalars().first()

        # temp signer with a known password
        await db.execute(delete(User).where(User.email == TEMP_EMAIL))
        await db.flush()
        signer = User(email=TEMP_EMAIL, name="E-Sign Verify Officer", passwordHash=hash_password(TEMP_PW),
                      role="QA_MANAGER", plantId=lms.id, department="Quality Assurance")
        db.add(signer)
        await db.flush()
        print(f"Plant {lms.code} | temp signer = {signer.name}")

        # ── 1. Trending ──
        tr = await dsvc.trending(db, plant_id=lms.id)
        print(f"\nTrending: total={tr['total']} open={tr['open']} overdueInvest={tr['overdueInvestigations']} "
              f"recurring={tr['recurring']} closureInSLA={tr['closureInSlaRate']}%")
        ok(tr["total"] >= 14, "trending counts seeded deviations")
        ok(len(tr["byCategory"]) >= 5 and len(tr["byMonth"]) >= 4, "trending spans categories + months")

        # ── 2. Workflow E2E ──
        print("\nWorkflow E2E (create -> classify -> investigate -> disposition -> close):")
        dev = await dsvc.create_deviation(db, user=signer, data={
            "plantId": lms.id, "title": "E2E verify deviation", "category": "laboratory",
            "severity": "major", "description": "Throwaway deviation for automated verification of the workflow.",
            "department": "QC", "area": "QC Lab",
        })
        await db.flush()
        ok(dev.status == "submitted", f"created -> submitted ({dev.number})")

        await dsvc.qa_classify(db, dev=dev, user=signer, severity="major", investigator_user_id=signer.id)
        await db.flush()
        ok(dev.status == "investigation_in_progress" and dev.investigationDueDate is not None, "classified -> investigation_in_progress, SLA due set")

        await dsvc.record_investigation(db, dev=dev, user=signer, root_cause_category="method_procedure",
                                        root_cause_description="SOP step ambiguous.", capa_required=False)
        await db.flush()
        ok(dev.status == "investigation_complete_pending_qa_review", "investigated -> pending QA review")

        # disposition — WRONG password must be rejected, nothing signed
        rejected = False
        try:
            await dsvc.record_disposition(db, dev=dev, user=signer, recommendation="reject",
                                          justification="bad result", password="WRONG-PASSWORD")
        except part11.SignatureError:
            rejected = True
        ok(rejected, "disposition with WRONG password -> SignatureError (re-auth enforced)")
        sigs0 = await part11.signatures_for(db, "deviation", dev.id)
        ok(len(sigs0) == 0, "no signature written on failed re-auth")

        # disposition — CORRECT password signs
        await dsvc.record_disposition(db, dev=dev, user=signer, recommendation="reject",
                                      justification="Confirmed OOS, batch rejected.", password=TEMP_PW)
        await db.flush()
        await dsvc.close_deviation(db, dev=dev, user=signer, password=TEMP_PW)
        await db.flush()
        ok(dev.status == "closed_no_capa", "disposition + close signed -> closed_no_capa")

        snap = part11.deviation_snapshot(dev)
        sigs = await part11.signatures_for(db, "deviation", dev.id, current_snapshot=snap)
        ok(len(sigs) == 2 and all(s["isValid"] for s in sigs), "2 valid signatures (disposition + closure)")

        # ── 3. Tamper-evidence ──
        print("\nPart 11 integrity:")
        dev.title = "E2E verify deviation — EDITED AFTER SIGNING"
        await db.flush()
        snap2 = part11.deviation_snapshot(dev)
        sigs_after = await part11.signatures_for(db, "deviation", dev.id, current_snapshot=snap2)
        ok(all(not s["isValid"] for s in sigs_after), "editing a signed record INVALIDATES its signatures")

        audit = await part11.audit_for(db, "deviation", dev.id)
        ok(len(audit) >= 5 and all(a["intact"] for a in audit), f"all {len(audit)} GMP audit entries verify intact")
        tampered = (await db.execute(select(GmpAuditEntry).where(GmpAuditEntry.recordId == dev.id).limit(1))).scalar_one()
        original = tampered.newValue
        tampered.newValue = "TAMPERED"
        ok(not part11.audit_entry_is_intact(tampered), "tampering an audit entry breaks its hash")
        tampered.newValue = original  # restore before discarding

        # ── 4. Real CAPA link in seed ──
        linked = (await db.execute(
            select(Deviation).where(Deviation.plantId == lms.id).where(Deviation.status == "closed_with_capa").limit(1)
        )).scalar_one_or_none()
        ok(linked is not None and linked.capaId is not None, "a seeded deviation is closed_with_capa + has capaId")
        capa = await db.get(Capa, linked.capaId)
        ok(capa is not None and capa.sourceReferenceId == linked.id, f"linked CAPA {linked.capaNumber} is a real row, back-linked to the deviation")

        # ── cleanup throwaway ──
        await db.execute(delete(ElectronicSignature).where(ElectronicSignature.recordId == dev.id))
        await db.execute(delete(GmpAuditEntry).where(GmpAuditEntry.recordId == dev.id))
        await db.execute(delete(Deviation).where(Deviation.id == dev.id))
        await db.execute(delete(User).where(User.id == signer.id))
        await db.commit()
        print("  cleaned up throwaway deviation + temp signer")

    print("\nAll pharma (Deviation + Part 11) checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
