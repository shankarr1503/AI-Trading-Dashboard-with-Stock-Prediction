"""Authentication router — register, login, refresh token, current user."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_current_user
from backend.auth.schemas import RefreshTokenRequest, Token, UserCreate, UserLogin, UserResponse
from backend.auth.utils import create_access_token, create_refresh_token, decode_token, get_password_hash, verify_password
from backend.config import settings
from backend.database.models import User
from backend.database.session import get_db
from backend.ratelimit import limiter

router = APIRouter()


# Verifying against a real hash when the email is unknown keeps login timing
# the same for existing and non-existing accounts (no account enumeration).
_DUMMY_HASH = get_password_hash("timing-equaliser-not-a-password")


def _issue_tokens(user: User) -> Token:
    claims = {"sub": str(user.id), "ver": user.token_version or 0}
    return Token(
        access_token=create_access_token(claims),
        refresh_token=create_refresh_token(claims),
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def register(request: Request, user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Register a new user. The administrator is the account whose email matches
    BOOTSTRAP_ADMIN_EMAIL; without it, only in development does the first
    account become admin (otherwise use `python -m backend.manage create-admin`).
    """
    if not settings.REGISTRATION_OPEN:
        raise HTTPException(status_code=403, detail="Registration is closed")
    existing = await db.execute(
        select(User).where(or_(User.email == user_data.email, User.username == user_data.username))
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Could not register with these details")

    bootstrap = settings.BOOTSTRAP_ADMIN_EMAIL.strip().lower()
    if bootstrap:
        make_admin = user_data.email.lower() == bootstrap
    elif settings.is_development:
        make_admin = (await db.scalar(select(func.count()).select_from(User))) == 0
    else:
        make_admin = False
    if make_admin and await db.scalar(select(func.count()).select_from(User).where(User.is_superuser.is_(True))):
        make_admin = False  # never mint a second admin through registration
    user = User(
        email=user_data.email,
        username=user_data.username,
        full_name=user_data.full_name,
        hashed_password=get_password_hash(user_data.password),
        is_superuser=make_admin,
    )
    db.add(user)
    try:
        await db.flush()
    except Exception:
        raise HTTPException(status_code=400, detail="Could not register with these details")
    return user


@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login(request: Request, credentials: UserLogin, db: AsyncSession = Depends(get_db)):
    """Login and receive JWT tokens."""
    result = await db.execute(select(User).where(User.email == credentials.email))
    user = result.scalar_one_or_none()
    password_ok = verify_password(credentials.password, user.hashed_password if user else _DUMMY_HASH)
    if not user or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return _issue_tokens(user)


@router.post("/refresh", response_model=Token)
@limiter.limit("30/minute")
async def refresh_token(request: Request, body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a refresh token for a new token pair."""
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    try:
        user = await db.get(User, int(payload.get("sub")))
    except (TypeError, ValueError):
        user = None
    if not user or not user.is_active or payload.get("ver", 0) != (user.token_version or 0):
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return _issue_tokens(user)


@router.post("/logout-all")
async def logout_all(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Revoke every access and refresh token issued to this account."""
    db_user = await db.get(User, user.id)
    db_user.token_version = (db_user.token_version or 0) + 1
    return {"message": "All sessions revoked"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(user: User = Depends(get_current_user)):
    """Get the authenticated user's profile."""
    return user
