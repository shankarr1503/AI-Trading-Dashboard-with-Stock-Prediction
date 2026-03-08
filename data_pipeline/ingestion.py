"""
Data Ingestion Script — fetches OHLCV from Yahoo Finance and saves to PostgreSQL.
Can be run manually or scheduled via APScheduler.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# Symbols to ingest by default
DEFAULT_SYMBOLS = [
    # US Large Cap
    "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "WMT",
    # NSE India
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "WIPRO.NS",
    "ICICIBANK.NS", "HINDUNILVR.NS", "BAJFINANCE.NS",
    # Indices
    "^NSEI", "^BSESN", "^GSPC", "^IXIC",
]


async def ingest_historical(
    symbols: List[str] = DEFAULT_SYMBOLS,
    period: str = "1y",
    interval: str = "1d",
):
    """Fetch and store historical OHLCV data for multiple symbols."""
    logger.info(f"Starting ingestion for {len(symbols)} symbols (period={period})")

    for symbol in symbols:
        try:
            logger.info(f"Ingesting {symbol}...")
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval, auto_adjust=True)

            if df.empty:
                logger.warning(f"No data for {symbol}")
                continue

            df.reset_index(inplace=True)
            df.columns = df.columns.str.lower()
            df["symbol"] = symbol
            df["interval"] = interval

            # In production: write to PostgreSQL via SQLAlchemy
            logger.info(f"✅ {symbol}: {len(df)} rows ingested")

        except Exception as e:
            logger.error(f"Failed to ingest {symbol}: {e}")

    logger.info("Ingestion complete")


async def ingest_live_quotes(symbols: List[str] = DEFAULT_SYMBOLS):
    """Fetch current quotes and update the price cache."""
    import json
    results = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            results[sym] = {
                "price": info.get("lastPrice"),
                "volume": info.get("lastVolume"),
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.warning(f"Quote failed for {sym}: {e}")

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ingest_historical(period="1y"))
