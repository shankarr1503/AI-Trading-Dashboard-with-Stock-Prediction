"""Technical indicators API router."""
from fastapi import APIRouter, Query, HTTPException
from backend.indicators.service import indicator_service

router = APIRouter()


@router.get("/{symbol}")
async def get_indicators(
    symbol: str,
    period: str = Query("6mo", description="Data period: 1mo,3mo,6mo,1y,2y,5y"),
    interval: str = Query("1d", description="Candle interval: 1d,1wk,1mo"),
):
    """
    Compute all technical indicators for a given symbol.
    Returns OHLCV data alongside indicator values aligned by timestamp.
    Includes per-indicator BUY/SELL/NEUTRAL signals.
    """
    try:
        return await indicator_service.compute_all(symbol.upper(), period=period, interval=interval)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Indicator computation failed: {e}")
