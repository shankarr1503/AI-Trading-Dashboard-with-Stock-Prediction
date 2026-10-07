"""
Data ingestion — fetch daily OHLCV from Yahoo Finance and upsert into `market_data`.

Usage:
    python -m data_pipeline.ingestion                 # default universe, 1y
    python -m data_pipeline.ingestion --symbols AAPL,MSFT --period 5y
"""
import argparse
import asyncio
import logging
from typing import List

import pandas as pd
import yfinance as yf
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.database.models import MarketData
from backend.database.session import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "WMT",
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "WIPRO.NS",
    "^NSEI", "^BSESN", "^GSPC", "^IXIC",
]


def _fetch(symbol: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    ts_col = "date" if "date" in df.columns else "datetime"
    df["timestamp"] = pd.to_datetime(df[ts_col], utc=True)
    return df.dropna(subset=["open", "high", "low", "close"])


async def ingest_historical(symbols: List[str] = DEFAULT_SYMBOLS, period: str = "1y", interval: str = "1d") -> int:
    """Fetch and upsert OHLCV rows. Returns the number of rows written."""
    dialect = engine.dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    total = 0
    for symbol in symbols:
        try:
            df = await asyncio.to_thread(_fetch, symbol, period, interval)
            if df.empty:
                logger.warning("No data for %s", symbol)
                continue
            rows = [
                {
                    "symbol": symbol, "timestamp": r.timestamp.to_pydatetime(), "interval": interval,
                    "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close),
                    "adj_close": float(r.close), "volume": int(r.volume or 0),
                }
                for r in df.itertuples()
            ]
            stmt = insert(MarketData).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "timestamp", "interval"],
                set_={k: getattr(stmt.excluded, k) for k in ("open", "high", "low", "close", "adj_close", "volume")},
            )
            async with AsyncSessionLocal() as db:
                await db.execute(stmt)
                await db.commit()
            total += len(rows)
            logger.info("%s: %d rows upserted", symbol, len(rows))
        except Exception as e:
            logger.error("Failed to ingest %s: %s", symbol, e)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--period", default="1y")
    parser.add_argument("--interval", default="1d")
    args = parser.parse_args()
    n = asyncio.run(ingest_historical([s.strip() for s in args.symbols.split(",") if s.strip()], args.period, args.interval))
    logger.info("Ingestion complete: %d rows", n)
