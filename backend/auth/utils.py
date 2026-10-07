"""JWT and password utilities (PyJWT + bcrypt; python-jose and passlib are unmaintained)."""
from datetime import timedelta
from typing import Optional

import bcrypt
import jwt

from backend.config import settings
from backend.database.session import utcnow

# bcrypt only uses the first 72 bytes of a password.
_BCRYPT_MAX_BYTES = 72


def _encode_pw(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(_encode_pw(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(_encode_pw(password), bcrypt.gensalt()).decode("utf-8")


def _encode(data: dict, token_type: str, expires: timedelta) -> str:
    payload = data.copy()
    now = utcnow()
    payload.update({"exp": now + expires, "iat": now, "type": token_type})
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    return _encode(data, "access", expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(data: dict) -> str:
    return _encode(data, "refresh", timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS))


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
