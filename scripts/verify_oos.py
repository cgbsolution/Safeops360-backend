"""Verify OOS Investigation E2E (Pharma IMS Module 3).

create -> phase 1 (wrong pw rejected, right pw signs) -> phase 2 (spawns a REAL
Deviation) -> disposition -> closed. Asserts statuses, signature validity, and
the bidirectional deviation link. Throwaway rows + temp signer hard-deleted.
"""

from __future__ import annotations

import asyncio

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.deviation import Deviation  # noqa: E402
from app.models.oos import OosInvestigation  # noqa: E402
from app.models.part11 import ElectronicSignature, GmpAuditEntry  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import oos as svc  # noqa: E402
from app.services import part11  # noqa: E402

TEMP_EMAIL = "oos.verify@local.test"
TEMP_PW = "Oos@1234"


def ok(c, label):
    print(f"  [{'PASS' if c else 'FAIL'}] {label}")
    if not c:
        raise SystemExit(1)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        lms = (await db.execute(select(Plant).where(Plant.code == "LMS"))).scalar_one_or_none() or (await db.execute(select(Plant))).scalars().first()
        await db.execute(delete(User).where(User.email == TEMP_EMAIL))
        await db.flush()
        signer = User(email=TEMP_EMAIL, name="OOS Verify QA", passwordHash=hash_password(TEMP_PW), role="QA_MANAGER", plantId=lms.id, department="QC")
        db.add(signer)
        await db.flush()
        print(f"Plant {lms.code} | signer {signer.name}")

        print("\nE2E:")
        o = await svc.create_oos(db, user=signer, data={"plantId": lms.id, "productName": "Verify Product", "batchNumber": "VFY-001",
                                                        "testName": "Assay (HPLC)", "specificationLimit": "98-102%", "initialResult": "95.0%"})
        await db.flush()
        ok(o.status == "phase_1_in_progress", f"created -> phase_1_in_progress ({o.number})")

        bad = False
        try:
            await svc.record_phase1(db, o=o, user=signer, password="WRONG", ip=None, checks=[], assignable_cause_found=False,
                                    assignable_cause_description="", result_invalidated=False, retest_authorized=False,
                                    retest_results=[], conclusion="no_laboratory_error_proceeds_to_phase_2")
        except part11.SignatureError:
            bad = True
        ok(bad, "phase 1 WRONG password -> rejected")

        await svc.record_phase1(db, o=o, user=signer, password=TEMP_PW, ip=None, checks=[{"check_type": "calculation_review", "result": "no_error_found"}],
                                assignable_cause_found=False, assignable_cause_description="", result_invalidated=False,
                                retest_authorized=True, retest_results=[], conclusion="no_laboratory_error_proceeds_to_phase_2")
        await db.flush()
        ok(o.status == "phase_2_in_progress", "phase 1 signed (no lab error) -> phase_2_in_progress")

        await svc.record_phase2(db, o=o, user=signer, password=TEMP_PW, ip=None, root_cause_category="machine_equipment",
                                root_cause_description="Column equilibration insufficient.", conclusion="manufacturing_cause_identified",
                                spawn_deviation=True, deviation_severity="major")
        await db.flush()
        ok(o.status == "batch_disposition_pending" and o.deviationId is not None, "phase 2 signed -> spawned deviation, batch_disposition_pending")
        dev = await db.get(Deviation, o.deviationId)
        ok(dev is not None and dev.detectionMethod == "oos_investigation", f"real linked deviation {o.deviationNumber} exists")

        await svc.record_disposition(db, o=o, user=signer, password=TEMP_PW, ip=None, disposition="reject", justification="OOS confirmed; batch rejected.")
        await db.flush()
        ok(o.status == "closed", "disposition signed -> closed")

        sigs = await part11.signatures_for(db, "oos", o.id, current_snapshot=svc.oos_snapshot(o))
        ok(len(sigs) == 3 and all(x["isValid"] for x in sigs), "3 valid signatures (phase1/phase2/disposition)")

        # cleanup
        await db.execute(delete(GmpAuditEntry).where(GmpAuditEntry.recordId == o.id))
        await db.execute(delete(ElectronicSignature).where(ElectronicSignature.recordId == o.id))
        await db.execute(delete(GmpAuditEntry).where(GmpAuditEntry.recordId == dev.id))
        await db.execute(delete(ElectronicSignature).where(ElectronicSignature.recordId == dev.id))
        await db.execute(delete(Deviation).where(Deviation.id == dev.id))
        await db.execute(delete(OosInvestigation).where(OosInvestigation.id == o.id))
        await db.execute(delete(User).where(User.id == signer.id))
        await db.commit()
        print("  cleaned up throwaway OOS + spawned deviation + temp signer")

    print("\nAll OOS checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
