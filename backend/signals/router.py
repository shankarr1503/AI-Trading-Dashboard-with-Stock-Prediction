"""Signals router."""
import logging

from fastapi import APIRouter, HTTPException

from backend.signals.service import signal_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{symbol}")
async def get_signal(symbol: str):
    """
    BUY / SELL / HOLD signal from regime-weighted technical factors, the ML
    forecast and news sentiment — with stop, target, expectancy and a check
    that the expected edge clears round-trip transaction costs.

    DISCLAIMER: For educational analysis only. Not financial advice.
    """
    try:
        return await signal_service.generate_signal(symbol)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Signal generation failed for %s", symbol)
        raise HTTPException(status_code=500, detail="Signal generation failed")
