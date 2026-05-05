"""Auth router. Mounts at /api/auth.

Endpoints:
  POST /api/auth/login         — email + password → JWT
  GET  /api/auth/permissions   — bag of permission codes the caller holds

NextAuth on the frontend keeps the session cookie/JWT. Its credentials
provider POSTs here; the access_token returned is what NextAuth signs into
the session and what the frontend forwards as `Authorization: Bearer …` to
every other backend endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, PermissionsResponse, UserOut
from app.services.permissions import get_permissions

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    stmt = select(User).where(User.email == payload.email.lower())
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.passwordHash):
        # Constant-time-ish: don't leak which one failed
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    token = create_access_token(
        subject=user.id,
        extra_claims={"role": user.role, "plantId": user.plantId, "email": user.email},
    )
    return LoginResponse(
        access_token=token,
        user=UserOut(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            plantId=user.plantId,
            designation=user.designation,
            department=user.department,
        ),
    )


@router.get("/permissions", response_model=PermissionsResponse)
async def my_permissions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermissionsResponse:
    perms = await get_permissions(db, user.id)
    return PermissionsResponse(permissions=perms)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        plantId=user.plantId,
        designation=user.designation,
        department=user.department,
    )
