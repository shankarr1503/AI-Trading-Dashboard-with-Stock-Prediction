"""
Shared slowapi limiter (enforced globally via SlowAPIMiddleware in main.py).

Behind nginx every request arrives from the proxy's IP, so the client address
comes from X-Real-IP — but only when the direct peer is inside
TRUSTED_PROXY_CIDRS (the Docker network / loopback by default). nginx overwrites that header, so clients cannot spoof it.
If Redis is unavailable the limiter falls back to in-process counters instead
of failing every request (which would lock the operator out of /flatten).

Desktop app (DESKTOP_MODE): every client (the app window, the Chrome extension,
any other local process) connects from 127.0.0.1, so a per-IP bucket is one
bucket that anybody on the machine could exhaust to lock the owner out. There,
requests carrying a valid access token are counted per account instead
(rate_limit_key), and the auth endpoints count per account rather than per IP
(failed sign-ins, and refreshes with a genuine refresh token: hit_account_limit). Cross-site browser traffic never reaches
the limiter at all (main.DesktopOriginGuard).
"""
import ipaddress
import logging
from typing import Optional

from limits import parse
from slowapi import Limiter
from starlette.requests import HTTPConnection

from backend.config import settings

logger = logging.getLogger(__name__)

_TRUSTED = [ipaddress.ip_network(c.strip()) for c in settings.TRUSTED_PROXY_CIDRS.split(",") if c.strip()]


def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED)


def client_ip(request: HTTPConnection) -> str:
    peer = request.client.host if request.client else "unknown"
    real = request.headers.get("x-real-ip")
    if real and _is_trusted_proxy(peer):
        return real.strip()
    return peer


def _access_token_subject(request: HTTPConnection) -> Optional[str]:
    """The account id of a valid access token in the Authorization header (None otherwise)."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    from backend.auth.utils import decode_token     # lazy: auth.utils imports the database layer

    payload = decode_token(token.strip())
    if not payload or payload.get("type") != "access" or payload.get("sub") in (None, ""):
        return None
    return str(payload["sub"])


def rate_limit_key(request: HTTPConnection) -> str:
    """
    The bucket a request counts against: the client IP, except in desktop mode,
    where a request with a valid access token counts against its account (an
    attacker cannot forge one), so unauthenticated floods cannot starve the owner.
    """
    if settings.DESKTOP_MODE:
        subject = _access_token_subject(request)
        if subject is not None:
            return f"user:{subject}"
    return client_ip(request)


def desktop_mode() -> bool:
    """exempt_when= for the auth limits that desktop mode replaces with per-account limits."""
    return bool(settings.DESKTOP_MODE)


def account_limit_reached(scope: str, account: str, limit: str) -> bool:
    """
    True when `account` has used up `limit` for `scope` (counted with
    hit_account_limit). Nothing is counted here. Storage errors never block.
    """
    if not limiter.enabled:
        return False
    try:
        return not limiter.limiter.test(parse(limit), "account", scope, account)
    except Exception:       # pragma: no cover - storage failure
        logger.warning("Rate limit storage error; not limiting %s", scope, exc_info=True)
        return False


def hit_account_limit(scope: str, account: str, limit: str) -> bool:
    """
    Count one attempt of `scope` (e.g. "login") for `account` against `limit`
    ("10/minute"), in the limiter's storage. False when the limit is exceeded.
    Storage errors never block a request (like the limiter's swallow_errors).
    """
    if not limiter.enabled:
        return True
    try:
        return bool(limiter.limiter.hit(parse(limit), "account", scope, account))
    except Exception:       # pragma: no cover - storage failure
        logger.warning("Rate limit storage error; not limiting %s", scope, exc_info=True)
        return True


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    storage_uri=settings.REDIS_URL or "memory://",
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)
