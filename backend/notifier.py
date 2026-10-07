"""
Operator alerts: POST a JSON message to ALERT_WEBHOOK_URL (Slack/Discord/Teams-
compatible `{"text": ...}` payload). Always logs; never raises.
"""
import logging
from typing import Any, Dict, Optional

import httpx

from backend.config import settings

logger = logging.getLogger("alerts")

LEVEL_ICON = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}


async def notify(event: str, message: str, level: str = "warning", details: Optional[Dict[str, Any]] = None) -> bool:
    log = logger.error if level == "critical" else logger.warning if level == "warning" else logger.info
    log("[%s] %s %s", event, message, details or "")
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        return False
    text = f"{LEVEL_ICON.get(level, '')} [{settings.APP_NAME} · {settings.TRADING_MODE}] {event}: {message}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"text": text, "event": event, "level": level, "details": details or {}})
            return resp.status_code < 400
    except Exception as e:  # alerting must never break trading
        logger.warning("Alert delivery failed: %s", e)
        return False
