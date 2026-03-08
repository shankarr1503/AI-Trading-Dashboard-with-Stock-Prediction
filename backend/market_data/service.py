"""
Market Data Service — fetches OHLCV and live quotes from multiple sources.
Primary: yfinance (Yahoo Finance)
Secondary: Alpha Vantage, Finnhub
Caching: Redis with TTL
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import yfinance as yf
import pandas as pd
import httpx
import redis.asyncio as aioredis

from backend.config import settings

logger = logging.getLogger(__name__)

# Redis client (using a mock if Redis is unavailable)
try:
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
except Exception as e:
    logger.warning(f"Could not initialize Redis client: {e}")
    redis_client = None

CACHE_TTL = {
    "quote": 15,       # 15 seconds for live quotes
    "history": 300,    # 5 minutes for historical data
    "search": 3600,    # 1 hour for search results
    "info": 3600,      # 1 hour for company info
}


class MarketDataService:
    """Unified market data service with multi-source fallback and Redis caching."""

    # ─── Cache helpers ────────────────────────────────────────────────────────

    async def _get_cache(self, key: str) -> Optional[Any]:
        if not redis_client:
            return None
        try:
            data = await redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.warning(f"Redis GET failed: {e}")
            return None

    async def _set_cache(self, key: str, value: Any, ttl: int):
        if not redis_client:
            return
        try:
            await redis_client.setex(key, ttl, json.dumps(value, default=str))
        except Exception as e:
            logger.warning(f"Redis SET failed: {e}")

    # ─── Live Quote ───────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        """Get real-time quote for a symbol. Cached for 15 seconds."""
        cache_key = f"quote:{symbol.upper()}"
        cached = await self._get_cache(cache_key)
        if cached:
            return cached

        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            fast_info = ticker.fast_info

            quote = {
                "symbol": symbol.upper(),
                "name": info.get("longName", symbol),
                "exchange": info.get("exchange", ""),
                "currency": info.get("currency", "USD"),
                "current_price": fast_info.get("lastPrice") or info.get("currentPrice", 0),
                "previous_close": fast_info.get("previousClose") or info.get("previousClose", 0),
                "open": fast_info.get("open") or info.get("open", 0),
                "day_high": fast_info.get("dayHigh") or info.get("dayHigh", 0),
                "day_low": fast_info.get("dayLow") or info.get("dayLow", 0),
                "volume": fast_info.get("lastVolume") or info.get("volume", 0),
                "avg_volume": info.get("averageVolume", 0),
                "market_cap": info.get("marketCap", 0),
                "pe_ratio": info.get("trailingPE"),
                "eps": info.get("trailingEps"),
                "52_week_high": fast_info.get("yearHigh") or info.get("fiftyTwoWeekHigh"),
                "52_week_low": fast_info.get("yearLow") or info.get("fiftyTwoWeekLow"),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "bid": info.get("bid", 0),
                "ask": info.get("ask", 0),
                "bid_size": info.get("bidSize", 0),
                "ask_size": info.get("askSize", 0),
                "timestamp": datetime.utcnow().isoformat(),
            }

            # Compute change values
            if quote["previous_close"] and quote["current_price"]:
                quote["change"] = round(quote["current_price"] - quote["previous_close"], 4)
                quote["change_pct"] = round(
                    (quote["change"] / quote["previous_close"]) * 100, 4
                )
            else:
                quote["change"] = 0
                quote["change_pct"] = 0

            await self._set_cache(cache_key, quote, CACHE_TTL["quote"])
            return quote

        except Exception as e:
            logger.error(f"Failed to fetch quote for {symbol}: {e}")
            raise ValueError(f"Could not fetch quote for symbol '{symbol}': {e}")

    # ─── Historical OHLCV ─────────────────────────────────────────────────────

    async def get_history(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical OHLCV data.
        period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
        interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo
        """
        cache_key = f"history:{symbol.upper()}:{period}:{interval}"
        cached = await self._get_cache(cache_key)
        if cached:
            return cached

        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval, auto_adjust=True)

            if df.empty:
                raise ValueError(f"No historical data found for '{symbol}'")

            df.index = pd.to_datetime(df.index)
            records = []
            for ts, row in df.iterrows():
                records.append({
                    "timestamp": ts.isoformat(),
                    "open": round(float(row.get("Open", 0)), 4),
                    "high": round(float(row.get("High", 0)), 4),
                    "low": round(float(row.get("Low", 0)), 4),
                    "close": round(float(row.get("Close", 0)), 4),
                    "volume": int(row.get("Volume", 0)),
                })

            ttl = 60 if interval in ("1m", "2m", "5m") else CACHE_TTL["history"]
            await self._set_cache(cache_key, records, ttl)
            return records

        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}: {e}")
            raise ValueError(f"Could not fetch history for '{symbol}': {e}")

    # ─── Symbol Search ────────────────────────────────────────────────────────

    async def search_symbols(self, query: str) -> List[Dict[str, str]]:
        """Search for stock symbols by name or ticker."""
        cache_key = f"search:{query.lower()}"
        cached = await self._get_cache(cache_key)
        if cached:
            return cached

        # Use Finnhub if key available, else basic yfinance search
        if settings.FINNHUB_API_KEY:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{settings.FINNHUB_BASE_URL}/search",
                        params={"q": query, "token": settings.FINNHUB_API_KEY},
                        timeout=10,
                    )
                    data = resp.json()
                    results = [
                        {
                            "symbol": item["symbol"],
                            "name": item["description"],
                            "type": item.get("type", ""),
                            "exchange": item.get("primaryExchange", ""),
                        }
                        for item in data.get("result", [])[:15]
                    ]
                    await self._set_cache(cache_key, results, CACHE_TTL["search"])
                    return results
            except Exception as e:
                logger.warning(f"Finnhub search failed: {e}")

        # Fallback: predefined popular stocks matching query
        popular = [
            {"symbol": "AAPL", "name": "Apple Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "MSFT", "name": "Microsoft Corporation", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "GOOGL", "name": "Alphabet Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "AMZN", "name": "Amazon.com Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "TSLA", "name": "Tesla Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "NVDA", "name": "NVIDIA Corporation", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "META", "name": "Meta Platforms Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "RELIANCE.NS", "name": "Reliance Industries", "type": "stock", "exchange": "NSE"},
            {"symbol": "TCS.NS", "name": "Tata Consultancy Services", "type": "stock", "exchange": "NSE"},
            {"symbol": "INFY.NS", "name": "Infosys Ltd", "type": "stock", "exchange": "NSE"},
            {"symbol": "HDFCBANK.NS", "name": "HDFC Bank Ltd", "type": "stock", "exchange": "NSE"},
            {"symbol": "WIPRO.NS", "name": "Wipro Ltd", "type": "stock", "exchange": "NSE"},
        ]
        q = query.upper()
        results = [
            s for s in popular
            if q in s["symbol"].upper() or q in s["name"].upper()
        ][:10]
        await self._set_cache(cache_key, results, CACHE_TTL["search"])
        return results

    # ─── Market Movers ────────────────────────────────────────────────────────

    async def get_market_movers(self) -> Dict[str, List[Dict]]:
        """Get top gainers, losers, and most active stocks."""
        cache_key = "market:movers"
        cached = await self._get_cache(cache_key)
        if cached:
            return cached

        # Well-known liquid symbols for demo movers
        symbols = [
            "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META",
            "JPM", "BAC", "GS", "V", "MA", "JNJ", "PFE", "WMT",
        ]
        quotes = []
        for sym in symbols:
            try:
                q = await self.get_quote(sym)
                quotes.append(q)
            except Exception:
                continue

        sorted_by_change = sorted(quotes, key=lambda x: x.get("change_pct", 0))
        movers = {
            "gainers": [q for q in sorted_by_change if q.get("change_pct", 0) > 0][-5:][::-1],
            "losers": sorted_by_change[:5],
            "most_active": sorted(quotes, key=lambda x: x.get("volume", 0), reverse=True)[:5],
        }
        await self._set_cache(cache_key, movers, 60)
        return movers


market_data_service = MarketDataService()
