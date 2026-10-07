"""Technical indicators API router."""
import logging

from fastapi import APIRouter, HTTPException, Query

from backend.indicators.service import indicator_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{symbol}")
async def get_indicators(
    symbol: str,
    period: str = Query("6mo", description="1mo,3mo,6mo,1y,2y,5y"),
    interval: str = Query("1d", description="1d,1wk,1mo"),
):
    """
    Compute all technical indicators for a symbol. Returns OHLCV data with
    indicator values aligned by timestamp plus per-indicator BUY/SELL/NEUTRAL readings.
    """
    try:
        return await indicator_service.compute_all(symbol, period=period, interval=interval)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Indicator computation failed for %s", symbol)
        raise HTTPException(status_code=500, detail="Indicator computation failed")
