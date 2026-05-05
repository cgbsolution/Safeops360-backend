from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

# bcrypt has a 72-byte limit on the input password. bcryptjs (used by the
# Node side) silently truncates; bcrypt 4.1+ raises ValueError. To stay
# bug-compatible with whatever bcryptjs wrote into the User table, we pre-
# truncate the input ourselves before passlib touches it. truncate_error=
# False on the bcrypt scheme is also belt-and-suspenders.
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__truncate_error=False,
)


def _truncate_72(plain: str) -> str:
    """Match bcryptjs's silent-truncate behaviour. Encode in UTF-8, slice
    to 72 bytes, decode back ignoring incomplete tail bytes. Equivalent of
    bcryptjs's `await bcrypt.compare(password, hash)` for long passwords."""
    raw = plain.encode("utf-8")
    if len(raw) <= 72:
        return plain
    return raw[:72].decode("utf-8", errors="ignore")


def hash_password(plain: str) -> str:
    return pwd_context.hash(_truncate_72(plain))


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(_truncate_72(plain), hashed)
    except (ValueError, Exception):
        # Defensive: a malformed hash in the DB or a bcrypt-version mismatch
        # should never crash the login endpoint — return False so callers
        # treat it as a wrong password.
        return False


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    """Mint an access token. `subject` is the user id."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode + validate signature/expiry. Raises JWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


class InvalidTokenError(Exception):
    """Raised by deps.get_current_user when the token can't be trusted."""


def safe_decode(token: str) -> dict[str, Any]:
    try:
        return decode_token(token)
    except JWTError as e:
        raise InvalidTokenError(str(e)) from e
