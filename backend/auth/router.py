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


def _issue_tokens(user: User) -> Token:
    claims = {"sub": str(user.id)}
    return Token(
        access_token=create_access_token(claims),
        refresh_token=create_refresh_token(claims),
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def register(request: Request, user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    """Register a new user. The very first account becomes the administrator."""
    existing = await db.execute(
        select(User).where(or_(User.email == user_data.email, User.username == user_data.username))
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Email or username already registered")

    user_count = await db.scalar(select(func.count()).select_from(User))
    user = User(
        email=user_data.email,
        username=user_data.username,
        full_name=user_data.full_name,
        hashed_password=get_password_hash(user_data.password),
        is_superuser=(user_count == 0),
    )
    db.add(user)
    await db.flush()
    return user


@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login(request: Request, credentials: UserLogin, db: AsyncSession = Depends(get_db)):
    """Login and receive JWT tokens."""
    result = await db.execute(select(User).where(User.email == credentials.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(credentials.password, user.hashed_password):
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
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return _issue_tokens(user)


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(user: User = Depends(get_current_user)):
    """Get the authenticated user's profile."""
    return user
