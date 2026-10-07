"""
Market Data Service — live quotes and OHLCV history.

Primary source: yfinance. yfinance is a blocking library, so every call runs in
a worker thread (asyncio.to_thread) to keep the event loop — and the WebSocket
streams on it — responsive. Results are cached (Redis or in-process).
"""
import asyncio
import logging
import math
import re
from typing import Any, Dict, List

import httpx
import pandas as pd
import yfinance as yf

from backend.cache import cache_get, cache_set
from backend.config import settings
from backend.database.session import utcnow

logger = logging.getLogger(__name__)

CACHE_TTL = {"quote": 15, "history": 300, "history_intraday": 60, "search": 3600, "movers": 60}
VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}
_SYMBOL_RE = re.compile(r"^[A-Z0-9^.\-=]{1,20}$")

# Limit concurrent yfinance calls so a burst of requests can't exhaust threads
# or trip Yahoo's rate limiting.
_YF_SEMAPHORE = asyncio.Semaphore(8)


def validate_symbol(symbol: str) -> str:
    sym = symbol.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(f"Invalid symbol '{symbol}'")
    return sym


def _num(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        return default if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return default


def _fetch_quote_sync(symbol: str) -> Dict[str, Any]:
    ticker = yf.Ticker(symbol)
    fast = ticker.fast_info
    try:
        info = ticker.info or {}
    except Exception:  # .info is slow and flaky; fast_info has the essentials
        info = {}

    def fast_get(key: str):
        try:
            return fast.get(key) if hasattr(fast, "get") else getattr(fast, key, None)
        except Exception:
            return None

    price = _num(fast_get("lastPrice") or info.get("currentPrice") or info.get("regularMarketPrice"))
    prev = _num(fast_get("previousClose") or info.get("previousClose"))
    if price <= 0:
        raise ValueError(f"No price available for '{symbol}'")
    quote = {
        "symbol": symbol,
        "name": info.get("longName") or info.get("shortName") or symbol,
        "exchange": info.get("exchange", ""),
        "currency": info.get("currency") or fast_get("currency") or "USD",
        "current_price": round(price, 4),
        "previous_close": round(prev, 4),
        "open": _num(fast_get("open") or info.get("open")),
        "day_high": _num(fast_get("dayHigh") or info.get("dayHigh")),
        "day_low": _num(fast_get("dayLow") or info.get("dayLow")),
        "volume": int(_num(fast_get("lastVolume") or info.get("volume"))),
        "avg_volume": int(_num(info.get("averageVolume"))),
        "market_cap": _num(fast_get("marketCap") or info.get("marketCap")),
        "pe_ratio": info.get("trailingPE"),
        "eps": info.get("trailingEps"),
        "52_week_high": _num(fast_get("yearHigh") or info.get("fiftyTwoWeekHigh")) or None,
        "52_week_low": _num(fast_get("yearLow") or info.get("fiftyTwoWeekLow")) or None,
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "bid": _num(info.get("bid")),
        "ask": _num(info.get("ask")),
        "bid_size": int(_num(info.get("bidSize"))),
        "ask_size": int(_num(info.get("askSize"))),
        "timestamp": utcnow().isoformat(),
    }
    if prev > 0:
        quote["change"] = round(price - prev, 4)
        quote["change_pct"] = round((price - prev) / prev * 100, 4)
    else:
        quote["change"] = 0.0
        quote["change_pct"] = 0.0
    return quote


def _fetch_history_sync(symbol: str, period: str, interval: str) -> List[Dict[str, Any]]:
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"No historical data found for '{symbol}'")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df.index = pd.to_datetime(df.index)
    return [
        {
            "timestamp": ts.isoformat(),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(_num(row.get("Volume", 0))),
        }
        for ts, row in df.iterrows()
    ]


def _fetch_news_sync(symbol: str) -> List[str]:
    items = yf.Ticker(symbol).news or []
    headlines = []
    for item in items[:20]:
        # yfinance has changed this payload shape across versions.
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title") or ""
        summary = content.get("summary") or content.get("description") or ""
        if title:
            headlines.append(f"{title}. {summary}".strip())
    return headlines


class MarketDataService:
    """Unified market data access with caching and non-blocking I/O."""

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        symbol = validate_symbol(symbol)
        key = f"quote:{symbol}"
        cached = await cache_get(key)
        if cached is not None:
            return cached
        try:
            async with _YF_SEMAPHORE:
                quote = await asyncio.to_thread(_fetch_quote_sync, symbol)
        except ValueError:
            raise
        except Exception as e:
            logger.warning("Quote fetch failed for %s: %s", symbol, e)
            raise ValueError(f"Could not fetch quote for '{symbol}'") from e
        await cache_set(key, quote, CACHE_TTL["quote"])
        return quote

    async def get_history(self, symbol: str, period: str = "1y", interval: str = "1d") -> List[Dict[str, Any]]:
        symbol = validate_symbol(symbol)
        if period not in VALID_PERIODS:
            raise ValueError(f"Invalid period '{period}'")
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Invalid interval '{interval}'")
        key = f"history:{symbol}:{period}:{interval}"
        cached = await cache_get(key)
        if cached is not None:
            return cached
        try:
            async with _YF_SEMAPHORE:
                records = await asyncio.to_thread(_fetch_history_sync, symbol, period, interval)
        except ValueError:
            raise
        except Exception as e:
            logger.warning("History fetch failed for %s: %s", symbol, e)
            raise ValueError(f"Could not fetch history for '{symbol}'") from e
        intraday = interval in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h")
        await cache_set(key, records, CACHE_TTL["history_intraday" if intraday else "history"])
        return records

    async def get_history_df(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        """History as a DataFrame indexed by timestamp with lower-case OHLCV columns."""
        records = await self.get_history(symbol, period, interval)
        df = pd.DataFrame(records)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp").astype(float)

    async def get_news(self, symbol: str) -> List[str]:
        symbol = validate_symbol(symbol)
        key = f"news:{symbol}"
        cached = await cache_get(key)
        if cached is not None:
            return cached
        try:
            async with _YF_SEMAPHORE:
                headlines = await asyncio.to_thread(_fetch_news_sync, symbol)
        except Exception as e:
            logger.info("News fetch failed for %s: %s", symbol, e)
            headlines = []
        await cache_set(key, headlines, 900)
        return headlines

    async def search_symbols(self, query: str) -> List[Dict[str, str]]:
        query = query.strip()[:50]
        key = f"search:{query.lower()}"
        cached = await cache_get(key)
        if cached is not None:
            return cached

        if settings.FINNHUB_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{settings.FINNHUB_BASE_URL}/search",
                        params={"q": query, "token": settings.FINNHUB_API_KEY},
                    )
                    resp.raise_for_status()
                    results = [
                        {
                            "symbol": item["symbol"],
                            "name": item.get("description", ""),
                            "type": item.get("type", ""),
                            "exchange": item.get("primaryExchange", ""),
                        }
                        for item in resp.json().get("result", [])[:15]
                    ]
                    await cache_set(key, results, CACHE_TTL["search"])
                    return results
            except Exception as e:
                logger.warning("Finnhub search failed: %s", e)

        popular = [
            {"symbol": "AAPL", "name": "Apple Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "MSFT", "name": "Microsoft Corporation", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "GOOGL", "name": "Alphabet Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "AMZN", "name": "Amazon.com Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "TSLA", "name": "Tesla Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "NVDA", "name": "NVIDIA Corporation", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "META", "name": "Meta Platforms Inc.", "type": "stock", "exchange": "NASDAQ"},
            {"symbol": "JPM", "name": "JPMorgan Chase & Co.", "type": "stock", "exchange": "NYSE"},
            {"symbol": "RELIANCE.NS", "name": "Reliance Industries", "type": "stock", "exchange": "NSE"},
            {"symbol": "TCS.NS", "name": "Tata Consultancy Services", "type": "stock", "exchange": "NSE"},
            {"symbol": "INFY.NS", "name": "Infosys Ltd", "type": "stock", "exchange": "NSE"},
            {"symbol": "HDFCBANK.NS", "name": "HDFC Bank Ltd", "type": "stock", "exchange": "NSE"},
            {"symbol": "WIPRO.NS", "name": "Wipro Ltd", "type": "stock", "exchange": "NSE"},
        ]
        q = query.upper()
        results = [s for s in popular if q in s["symbol"] or q in s["name"].upper()][:10]
        await cache_set(key, results, CACHE_TTL["search"])
        return results

    async def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch several quotes concurrently; failures map to {'error': ...}."""
        async def one(sym: str):
            try:
                return sym, await self.get_quote(sym)
            except ValueError as e:
                return sym, {"error": str(e)}

        return dict(await asyncio.gather(*(one(s) for s in symbols)))

    async def get_market_movers(self) -> Dict[str, List[Dict]]:
        key = "market:movers"
        cached = await cache_get(key)
        if cached is not None:
            return cached
        symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META",
                   "JPM", "BAC", "GS", "V", "MA", "JNJ", "PFE", "WMT"]
        quotes = [q for q in (await self.get_quotes(symbols)).values() if "error" not in q]
        by_change = sorted(quotes, key=lambda x: x.get("change_pct", 0))
        movers = {
            "gainers": [q for q in reversed(by_change) if q.get("change_pct", 0) > 0][:5],
            "losers": [q for q in by_change if q.get("change_pct", 0) < 0][:5],
            "most_active": sorted(quotes, key=lambda x: x.get("volume", 0), reverse=True)[:5],
        }
        if quotes:
            await cache_set(key, movers, CACHE_TTL["movers"])
        return movers


market_data_service = MarketDataService()
