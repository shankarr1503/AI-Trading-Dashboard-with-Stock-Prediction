"""Market data API router."""
from typing import Optional, List
from fastapi import APIRouter, Query, HTTPException
from backend.market_data.service import market_data_service

router = APIRouter()


@router.get("/quote/{symbol}")
async def get_quote(symbol: str):
    """Get real-time quote for a stock symbol."""
    try:
        return await market_data_service.get_quote(symbol.upper())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/history/{symbol}")
async def get_history(
    symbol: str,
    period: str = Query("1y", description="Data period: 1d,5d,1mo,3mo,6mo,1y,2y,5y,max"),
    interval: str = Query("1d", description="Candle interval: 1m,5m,15m,1h,1d,1wk,1mo"),
):
    """Get historical OHLCV candlestick data."""
    try:
        return await market_data_service.get_history(symbol.upper(), period, interval)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/search")
async def search_symbols(q: str = Query(..., min_length=1, description="Symbol or company name")):
    """Search for stock symbols."""
    return await market_data_service.search_symbols(q)


@router.get("/movers")
async def get_market_movers():
    """Get top gainers, losers, and most active stocks."""
    return await market_data_service.get_market_movers()


@router.get("/batch")
async def get_batch_quotes(
    symbols: str = Query(..., description="Comma-separated symbol list, e.g. AAPL,MSFT,GOOGL")
):
    """Fetch quotes for multiple symbols at once."""
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if len(symbol_list) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 symbols per batch request")

    results = {}
    for sym in symbol_list:
        try:
            results[sym] = await market_data_service.get_quote(sym)
        except Exception as e:
            results[sym] = {"error": str(e)}
    return results
