"""21 CFR Part 11 router — electronic signatures + GMP audit trail.

Mounts at /api/esign and /api/gmp-audit. Reusable across GMP modules:
  POST /api/esign            apply a re-authenticated electronic signature
  GET  /api/esign            list a record's signatures (validity recomputed)
  GET  /api/gmp-audit        list a record's immutable audit trail
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services import part11

router = APIRouter(tags=["part11"])


class SignRequest(BaseModel):
    recordType: str
    recordId: str
    recordNumber: str | None = None
    signatureMeaning: str
    password: str


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/api/esign", status_code=status.HTTP_201_CREATED)
async def sign(
    body: SignRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Apply an electronic signature. The signer must re-enter their password;
    it is verified server-side against the bcrypt hash (21 CFR 11.200)."""
    snapshot = await part11.snapshot_for(db, body.recordType, body.recordId)
    try:
        sig = await part11.verify_and_sign(
            db,
            user=user,
            record_type=body.recordType,
            record_id=body.recordId,
            record_number=body.recordNumber,
            meaning=body.signatureMeaning,
            password=body.password,
            record_snapshot=snapshot,
            ip=_client_ip(request),
        )
    except part11.SignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
    await db.commit()
    return {
        "id": sig.id,
        "signerFullName": sig.signerFullName,
        "signerRole": sig.signerRole,
        "signatureMeaning": sig.signatureMeaning,
        "signedAt": sig.signedAt.isoformat() if sig.signedAt else None,
        "isValid": sig.isValid,
    }


@router.get("/api/esign")
async def list_signatures(
    recordType: str = Query(...),
    recordId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    snapshot = await part11.snapshot_for(db, recordType, recordId)
    sigs = await part11.signatures_for(db, recordType, recordId, current_snapshot=snapshot)
    return {"recordType": recordType, "recordId": recordId, "signatures": sigs}


@router.get("/api/gmp-audit")
async def list_audit(
    recordType: str = Query(...),
    recordId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entries = await part11.audit_for(db, recordType, recordId)
    return {"recordType": recordType, "recordId": recordId, "entries": entries}
