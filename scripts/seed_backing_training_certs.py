"""Seed TrainingCertificates that back the Skill Matrix's training-based cells.

Makes the matrix coherent once it derives from training: for every cell on a
training-fed competency that is *training*-validated / expiring / pending, we
create a matching active certificate so the receiver keeps it green instead of
flipping it to "expired" for lack of evidence. Assessment-validated cells are
left alone (they're legitimately valid without a training cert).

Idempotent — skips a (person, program) pair that already has a live cert.

    .venv/Scripts/python.exe scripts/seed_backing_training_certs.py [PLANT_ID]
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.competency_matrix import Competency, CompetencyRecord
from app.models.training import TrainingCertificate, TrainingProgram

PLANT_ID = sys.argv[1] if len(sys.argv) > 1 else "cmpwi9h8200098nbk3nxzcdjf"

# Cells worth backing with a training certificate.
TRAINING_STATES = {"validated_active", "expiring_soon", "training_complete_pending_assessment"}
LIVE_CERT_STATES = ("ACTIVE", "EXPIRING_SOON")


def main() -> int:
    eng = create_engine(get_settings().sync_database_url, future=True)
    with Session(eng) as s:
        # Map each training-fed competency to its (first) program row.
        comps = (
            s.execute(select(Competency).where(Competency.relatedTrainingProgramIds.isnot(None)))
        ).scalars().all()
        prog_by_comp: dict[str, TrainingProgram] = {}
        for c in comps:
            codes = c.relatedTrainingProgramIds or []
            if not codes:
                continue
            prog = (
                s.execute(select(TrainingProgram).where(TrainingProgram.code == codes[0]))
            ).scalar_one_or_none()
            if prog is not None:
                prog_by_comp[c.id] = prog

        records = (
            s.execute(
                select(CompetencyRecord)
                .where(CompetencyRecord.plantId == PLANT_ID)
                .where(CompetencyRecord.competencyId.in_(list(prog_by_comp.keys())))
                .where(CompetencyRecord.state.in_(TRAINING_STATES))
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        created = 0
        skipped_assessment = 0
        skipped_existing = 0

        for r in records:
            # A 'validated_active' cell that was validated by assessment doesn't
            # need (and shouldn't get) a fabricated training cert.
            if r.state == "validated_active" and r.currentValidationMethod not in (
                "training_completion",
                None,
            ):
                skipped_assessment += 1
                continue

            prog = prog_by_comp[r.competencyId]

            existing = (
                s.execute(
                    select(TrainingCertificate)
                    .where(TrainingCertificate.userId == r.personUserId)
                    .where(TrainingCertificate.programId == prog.id)
                    .where(TrainingCertificate.status.in_(LIVE_CERT_STATES))
                )
            ).first()
            if existing:
                skipped_existing += 1
                continue

            expiring = r.state == "expiring_soon"
            valid_from = now - timedelta(days=60)
            valid_to = now + timedelta(days=20 if expiring else 365)
            s.add(
                TrainingCertificate(
                    certificateNumber=f"CERT-{prog.code}-{r.personUserId[-10:]}-{uuid4().hex[:6]}",
                    programId=prog.id,
                    userId=r.personUserId,
                    issuedAt=valid_from,
                    validFrom=valid_from,
                    validTo=valid_to,
                    status="EXPIRING_SOON" if expiring else "ACTIVE",
                    finalAssessmentScore=85.0,
                    isRenewable=True,
                )
            )
            created += 1

        s.commit()
        print(
            f"Plant {PLANT_ID}\n"
            f"  training-based cells scanned: {len(records)}\n"
            f"  certificates created:         {created}\n"
            f"  skipped (assessment-valid):   {skipped_assessment}\n"
            f"  skipped (cert already live):  {skipped_existing}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
