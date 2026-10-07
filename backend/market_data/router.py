"""Market data API router."""
from fastapi import APIRouter, HTTPException, Query, Request

from backend.market_data.service import market_data_service, validate_symbol
from backend.ratelimit import limiter

router = APIRouter()


@router.get("/quote/{symbol}")
async def get_quote(symbol: str):
    """Get the latest quote for a stock symbol."""
    try:
        return await market_data_service.get_quote(symbol)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/history/{symbol}")
@limiter.limit("30/minute")
async def get_history(
    request: Request,
    symbol: str,
    period: str = Query("1y", description="1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max"),
    interval: str = Query("1d", description="1m,5m,15m,30m,1h,1d,1wk,1mo"),
):
    """Get historical OHLCV candlestick data."""
    try:
        return await market_data_service.get_history(symbol, period, interval)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/search")
async def search_symbols(q: str = Query(..., min_length=1, max_length=50)):
    """Search for stock symbols."""
    return await market_data_service.search_symbols(q)


@router.get("/movers")
async def get_market_movers():
    """Top gainers, losers and most active stocks from a liquid reference basket."""
    return await market_data_service.get_market_movers()


@router.get("/batch")
@limiter.limit("30/minute")
async def get_batch_quotes(request: Request, symbols: str = Query(..., description="Comma-separated, e.g. AAPL,MSFT")):
    """
    Fetch quotes for up to 20 symbols concurrently. Rate-limited harder than the
    default: every uncached symbol is an upstream fetch on the egress IP the bot shares.
    """
    raw = [s for s in symbols.split(",") if s.strip()]
    if len(raw) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 symbols per batch request")
    try:
        symbol_list = [validate_symbol(s) for s in raw]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await market_data_service.get_quotes(symbol_list)
