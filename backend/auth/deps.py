"""Authentication dependencies for protected routes."""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.utils import decode_token
from backend.database.models import User
from backend.database.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHORIZED
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise _UNAUTHORIZED
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise _UNAUTHORIZED
    user = await db.get(User, user_id)
    if user is None or not user.is_active or payload.get("ver", 0) != (user.token_version or 0):
        raise _UNAUTHORIZED
    return user


async def get_current_superuser(user: User = Depends(get_current_user)) -> User:
    if not user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user
