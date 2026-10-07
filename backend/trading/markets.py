"""Exchange session hours (regular sessions only; exchange holidays are not modelled)."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

US_TZ = ZoneInfo("America/New_York")
IN_TZ = ZoneInfo("Asia/Kolkata")


def exchange_for(symbol: str) -> str:
    s = symbol.upper()
    return "NSE" if s.endswith(".NS") or s.endswith(".BO") else "US"


def is_regular_session(symbol: str, now: datetime) -> bool:
    if exchange_for(symbol) == "NSE":
        local = now.astimezone(IN_TZ)
        start, end = time(9, 15), time(15, 30)
    else:
        local = now.astimezone(US_TZ)
        start, end = time(9, 30), time(16, 0)
    return local.weekday() < 5 and start <= local.time() < end


def trading_date(now: datetime) -> str:
    """The US trading date used for daily loss accounting."""
    return now.astimezone(US_TZ).date().isoformat()
