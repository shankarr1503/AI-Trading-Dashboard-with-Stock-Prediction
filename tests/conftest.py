"""Shared test fixtures. Everything runs offline on synthetic market data."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="trading-tests-")
os.environ.update({
    "APP_ENV": "test",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP}/test.db",
    "REDIS_URL": "",
    "JWT_SECRET_KEY": "test-secret-key-that-is-long-enough-123456",
    "ML_SERVICE_URL": "http://127.0.0.1:9",  # nothing listens here → fallbacks are exercised
    "LLM_REVIEW_ENABLED": "false",
    "TRADING_MODE": "paper",
    "BOT_UNIVERSE": "AAA,BBB,CCC",
})

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from backend.cache import cache_clear_local  # noqa: E402
from backend.database import models  # noqa: E402,F401
from backend.database.session import Base, engine  # noqa: E402
from backend.ratelimit import limiter  # noqa: E402


def make_ohlcv(seed: int = 0, n: int = 600, drift: float = 0.0004, vol: float = 0.015,
               regime_switch: bool = True, start: str = "2021-01-01") -> pd.DataFrame:
    """Synthetic daily bars with realistic overnight gaps and optional drift regimes."""
    rng = np.random.default_rng(seed)
    if regime_switch:
        regimes = np.repeat(rng.choice([-1, 0, 1, 1], size=n // 100 + 1), 100)[:n]
    else:
        regimes = np.zeros(n)
    gap = rng.normal(0, vol * 0.5, n)
    intraday = rng.normal(drift + regimes * 0.0012, vol * 0.85, n)
    open_ = 100 * np.exp(np.cumsum(gap + np.r_[0, intraday[:-1]]))
    close = open_ * np.exp(intraday)
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.004, n)))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.bdate_range(start, periods=n, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def make_trending(seed: int = 0, n: int = 500) -> pd.DataFrame:
    """A steady, low-noise uptrend that the strategy should want to buy."""
    return make_ohlcv(seed=seed, n=n, drift=0.0025, vol=0.009, regime_switch=False)


class FakeMarket:
    """Stands in for MarketDataService (no network)."""

    def __init__(self, frames: dict):
        self.frames = frames
        self.price_override: dict = {}
        self.splits: dict = {}
        self.quote_failures: set = set()

    async def get_history_df(self, symbol, period="1y", interval="1d"):
        if symbol not in self.frames:
            raise ValueError(f"No data for {symbol}")
        return self.frames[symbol].copy()

    async def get_history(self, symbol, period="1y", interval="1d"):
        df = await self.get_history_df(symbol)
        return [{"timestamp": ts.isoformat(), **{k: float(row[k]) for k in ("open", "high", "low", "close")},
                 "volume": int(row["volume"])} for ts, row in df.iterrows()]

    async def get_quote(self, symbol):
        if symbol not in self.frames or symbol in self.quote_failures:
            raise ValueError(f"No quote for {symbol}")
        df = self.frames[symbol]
        price = self.price_override.get(symbol, float(df["close"].iloc[-1]))
        prev = float(df["close"].iloc[-2])
        return {"symbol": symbol, "current_price": price, "previous_close": prev,
                "change_pct": (price / prev - 1) * 100, "volume": int(df["volume"].iloc[-1]), "sector": "Tech"}

    async def get_quotes(self, symbols):
        out = {}
        for s in symbols:
            try:
                out[s] = await self.get_quote(s)
            except ValueError as e:
                out[s] = {"error": str(e)}
        return out

    async def get_news(self, symbol):
        return []

    async def get_splits(self, symbol):
        return self.splits.get(symbol, [])


class FakeResearch:
    """Stands in for ResearchService.fundamental_view."""

    def __init__(self, views: dict | None = None):
        self.views = views or {}

    async def fundamental_view(self, symbol):
        return self.views.get(symbol, {"available": False})


class FakePredictor:
    async def predict(self, symbol):
        raise ValueError("ML unavailable in tests")

    async def sentiment(self, symbol):
        return {"headlines_analyzed": 0, "compound_score": 0.0}


@pytest.fixture(autouse=True)
def _reset_global_state():
    cache_clear_local()
    limiter.reset()
    yield


@pytest.fixture
async def db_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
