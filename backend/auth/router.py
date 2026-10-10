"""Authentication router — register, login, refresh token, current user."""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_current_user
from backend.auth.schemas import RefreshTokenRequest, Token, UserCreate, UserLogin, UserResponse
from backend.auth.utils import create_access_token, create_refresh_token, decode_token, get_password_hash, verify_password
from backend.config import settings
from backend.database.models import User
from backend.database.session import get_db
from backend.ratelimit import account_limit_reached, desktop_mode, hit_account_limit, limiter

router = APIRouter()

# Desktop mode counts these per account instead of per IP (every client there is
# 127.0.0.1: a shared bucket would let any local process lock the owner out).
# Sign-ins count failed attempts only, so the owner's own sign-ins never use it up.
LOGIN_LIMIT = "10/minute"
REFRESH_LIMIT = "30/minute"
FIRST_ACCOUNT_DENIED = "Create the first account in the AI Trading Bot app window"


# Verifying against a real hash when the email is unknown keeps login timing
# the same for existing and non-existing accounts (no account enumeration).
_DUMMY_HASH = get_password_hash("timing-equaliser-not-a-password")


async def _user_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


def _from_the_app_window(request: Request) -> bool:
    """
    The desktop shell adds its per-launch control token (X-Desktop-Token) to the
    app window's own /auth/register requests, and to nothing else. Nobody else on
    the machine knows it: not a web page, not another local user.
    """
    expected = settings.TRADEBOT_CONTROL_TOKEN
    supplied = request.headers.get("x-desktop-token") or ""
    return bool(expected) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _too_many(detail: str = "Too many attempts: try again in a minute") -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": "60"})


def _issue_tokens(user: User) -> Token:
    claims = {"sub": str(user.id), "ver": user.token_version or 0}
    return Token(
        access_token=create_access_token(claims),
        refresh_token=create_refresh_token(claims),
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute", exempt_when=desktop_mode)
async def register(request: Request, user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Register a new user. Registration never grants admin rights outside
    development (no email verification exists, so a claimed address proves
    nothing): create the administrator with `python -m backend.manage create-admin`.
    In development the very first account becomes admin for convenience.

    The desktop app (DESKTOP_MODE) is single-user and only listens on 127.0.0.1:
    the first account is its owner and administrator, and registration closes
    as soon as any account exists, whatever REGISTRATION_OPEN says. When the
    Electron shell started the server (TRADEBOT_CONTROL_TOKEN is set), that first
    account can only be created from the app window: anything else on the machine
    (a web page, another OS account) could otherwise claim the administrator
    account before the owner. A sidecar started by hand without a control token
    (development from source) accepts the first registration from anyone who can
    reach 127.0.0.1.
    """
    if settings.DESKTOP_MODE:
        if await _user_count(db) > 0:
            raise HTTPException(status_code=403, detail="Registration is closed")
        if settings.TRADEBOT_CONTROL_TOKEN and not _from_the_app_window(request):
            raise HTTPException(status_code=403, detail=FIRST_ACCOUNT_DENIED)
    elif not settings.REGISTRATION_OPEN:
        raise HTTPException(status_code=403, detail="Registration is closed")
    email = user_data.email.strip().lower()
    existing = await db.execute(
        select(User).where(or_(func.lower(User.email) == email, User.username == user_data.username))
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Could not register with these details")

    if settings.DESKTOP_MODE:
        make_admin = True     # the count above was 0
    else:
        make_admin = settings.is_development and (await _user_count(db)) == 0
    user = User(
        email=email,
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
    if settings.DESKTOP_MODE and await _user_count(db) != 1:
        # Two "first" registrations raced: the later one must not become a second
        # owner. Raising makes get_db roll this insert back.
        raise HTTPException(status_code=403, detail="Registration is closed")
    return user


@router.post("/login", response_model=Token)
@limiter.limit(LOGIN_LIMIT, exempt_when=desktop_mode)
async def login(request: Request, credentials: UserLogin, db: AsyncSession = Depends(get_db)):
    """Login and receive JWT tokens (desktop mode: at most LOGIN_LIMIT failed attempts per account)."""
    email = credentials.email.strip().lower()
    if settings.DESKTOP_MODE and account_limit_reached("login", email, LOGIN_LIMIT):
        raise _too_many("Too many failed sign-in attempts for this account: try again in a minute")
    result = await db.execute(select(User).where(func.lower(User.email) == email))
    user = result.scalar_one_or_none()
    password_ok = verify_password(credentials.password, user.hashed_password if user else _DUMMY_HASH)
    if not user or not password_ok:
        if settings.DESKTOP_MODE:
            hit_account_limit("login", email, LOGIN_LIMIT)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return _issue_tokens(user)


@router.post("/refresh", response_model=Token)
@limiter.limit(REFRESH_LIMIT, exempt_when=desktop_mode)
async def refresh_token(request: Request, body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """
    Exchange a refresh token for a new token pair. Desktop mode counts refreshes
    per account; tokens without a valid signature are refused before counting, so
    bogus refreshes cannot use up the owner's allowance.
    """
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if settings.DESKTOP_MODE and not hit_account_limit("refresh", str(payload.get("sub")), REFRESH_LIMIT):
        raise _too_many()
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
