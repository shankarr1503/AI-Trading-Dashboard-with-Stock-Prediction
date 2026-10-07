"""
Small async cache: Redis when REDIS_URL is configured and reachable, otherwise an
in-process TTL dict. Cache failures never break a request.
"""
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from backend.config import settings

logger = logging.getLogger(__name__)

_redis = None
if settings.REDIS_URL:
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as e:  # pragma: no cover - depends on environment
        logger.warning("Redis unavailable, using in-memory cache: %s", e)
        _redis = None

_local: Dict[str, Tuple[float, str]] = {}
_LOCAL_MAX_KEYS = 5000


async def cache_get(key: str) -> Optional[Any]:
    if _redis is not None:
        try:
            raw = await _redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as e:
            logger.debug("Redis GET failed (%s); falling back to local cache", e)
    item = _local.get(key)
    if item is None:
        return None
    expires, raw = item
    if expires < time.monotonic():
        _local.pop(key, None)
        return None
    return json.loads(raw)


async def cache_set(key: str, value: Any, ttl: int) -> None:
    raw = json.dumps(value, default=str)
    if _redis is not None:
        try:
            await _redis.setex(key, ttl, raw)
            return
        except Exception as e:
            logger.debug("Redis SET failed (%s); using local cache", e)
    if len(_local) >= _LOCAL_MAX_KEYS:
        now = time.monotonic()
        for k in [k for k, (exp, _) in _local.items() if exp < now]:
            _local.pop(k, None)
        if len(_local) >= _LOCAL_MAX_KEYS:
            _local.pop(next(iter(_local)))
    _local[key] = (time.monotonic() + ttl, raw)


def cache_clear_local() -> None:
    _local.clear()
