"""
Shared slowapi limiter (enforced globally via SlowAPIMiddleware in main.py).

Behind nginx every request arrives from the proxy's IP, so the client address
comes from X-Real-IP — but only when the direct peer is inside
TRUSTED_PROXY_CIDRS (the Docker network / loopback by default). nginx overwrites that header, so clients cannot spoof it.
If Redis is unavailable the limiter falls back to in-process counters instead
of failing every request (which would lock the operator out of /flatten).
"""
import ipaddress

from slowapi import Limiter
from starlette.requests import Request

from backend.config import settings


_TRUSTED = [ipaddress.ip_network(c.strip()) for c in settings.TRUSTED_PROXY_CIDRS.split(",") if c.strip()]


def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    real = request.headers.get("x-real-ip")
    if real and _is_trusted_proxy(peer):
        return real.strip()
    return peer


limiter = Limiter(
    key_func=client_ip,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    storage_uri=settings.REDIS_URL or "memory://",
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)
