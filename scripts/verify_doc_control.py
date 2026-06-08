"""Verify Document Control lifecycle E2E (Pharma IMS Module 2).

create -> submit -> technical review (wrong pw rejected, right pw signs) ->
QA review -> approve -> make effective (supersedes) -> acknowledge -> revise.
Asserts statuses, signature validity, and supersession. Throwaway rows + temp
signer hard-deleted at the end.
"""

from __future__ import annotations

import asyncio

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.document_control import ControlledDocument, DocumentVersion  # noqa: E402
from app.models.part11 import ElectronicSignature, GmpAuditEntry  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import document_control as svc  # noqa: E402
from app.services import part11  # noqa: E402

TEMP_EMAIL = "doc.verify@local.test"
TEMP_PW = "Doc@1234"


def ok(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        lms = (await db.execute(select(Plant).where(Plant.code == "LMS"))).scalar_one_or_none() or (await db.execute(select(Plant))).scalars().first()
        await db.execute(delete(User).where(User.email == TEMP_EMAIL))
        await db.flush()
        signer = User(email=TEMP_EMAIL, name="Doc Verify QA", passwordHash=hash_password(TEMP_PW), role="QA_MANAGER", plantId=lms.id, department="QA")
        db.add(signer)
        await db.flush()
        print(f"Plant {lms.code} | signer {signer.name}")

        print("\nLifecycle E2E:")
        doc = await svc.create_document(db, user=signer, data={"plantId": lms.id, "title": "E2E verify SOP", "documentType": "sop", "category": "QA"})
        await db.flush()
        ok(doc.currentVersionStatus == "draft", f"created draft ({doc.documentNumber})")

        await svc.submit_for_review(db, doc=doc, user=signer)
        await db.flush()
        ok(doc.currentVersionStatus == "under_review", "submitted -> under_review")

        bad = False
        try:
            await svc.technical_review(db, doc=doc, user=signer, password="WRONG")
        except part11.SignatureError:
            bad = True
        ok(bad, "technical review WRONG password -> rejected")

        await svc.technical_review(db, doc=doc, user=signer, password=TEMP_PW)
        await svc.qa_review(db, doc=doc, user=signer, password=TEMP_PW)
        await svc.approve(db, doc=doc, user=signer, password=TEMP_PW)
        await db.flush()
        ok(doc.currentVersionStatus == "approved", "tech + QA + approve signed -> approved")

        await svc.make_effective(db, doc=doc, user=signer)
        await db.flush()
        ok(doc.currentVersionStatus == "effective" and doc.currentVersion == "1.0", "make effective")

        v = (await db.execute(select(DocumentVersion).where(DocumentVersion.documentId == doc.id).where(DocumentVersion.status == "effective"))).scalar_one()
        sigs = await part11.signatures_for(db, "document_version", v.id, current_snapshot=svc.version_snapshot(v, doc))
        ok(len(sigs) == 3 and all(x["isValid"] for x in sigs), "3 valid version signatures (technical/QA/approval)")

        await svc.acknowledge(db, doc=doc, user=signer, password=TEMP_PW)
        await db.flush()
        acks = await part11.signatures_for(db, "document_ack", doc.id)
        ok(len(acks) == 1 and acks[0]["isValid"], "read-acknowledgment signed + valid")

        newv = await svc.revise(db, doc=doc, user=signer, change_summary="Clarified step 4.2 per CAPA.")
        await svc.submit_for_review(db, doc=doc, user=signer)
        await svc.technical_review(db, doc=doc, user=signer, password=TEMP_PW)
        await svc.qa_review(db, doc=doc, user=signer, password=TEMP_PW)
        await svc.approve(db, doc=doc, user=signer, password=TEMP_PW)
        await svc.make_effective(db, doc=doc, user=signer)
        await db.flush()
        ok(doc.currentVersion == newv.version and doc.currentVersionStatus == "effective", f"revised to v{newv.version}, effective")
        old = (await db.execute(select(DocumentVersion).where(DocumentVersion.documentId == doc.id).where(DocumentVersion.version == "1.0"))).scalar_one()
        ok(old.status == "superseded", "prior version superseded")

        # cleanup
        for rt in ("document", "document_version", "document_ack"):
            await db.execute(delete(GmpAuditEntry).where(GmpAuditEntry.recordType == rt).where(GmpAuditEntry.recordId == doc.id))
            await db.execute(delete(ElectronicSignature).where(ElectronicSignature.recordType == rt).where(ElectronicSignature.recordId == doc.id))
        for vv in (await db.execute(select(DocumentVersion).where(DocumentVersion.documentId == doc.id))).scalars().all():
            await db.execute(delete(ElectronicSignature).where(ElectronicSignature.recordType == "document_version").where(ElectronicSignature.recordId == vv.id))
        await db.execute(delete(DocumentVersion).where(DocumentVersion.documentId == doc.id))
        await db.execute(delete(ControlledDocument).where(ControlledDocument.id == doc.id))
        await db.execute(delete(User).where(User.id == signer.id))
        await db.commit()
        print("  cleaned up throwaway document + temp signer")

    print("\nAll Document Control checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
