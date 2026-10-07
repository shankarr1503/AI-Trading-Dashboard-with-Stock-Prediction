"""
Technical Indicator Engine.
Computes SMA, EMA, RSI, MACD, Bollinger Bands, VWAP, ATR, Stochastic, Momentum,
Williams %R, ADX and OBV using the shared pure-pandas implementations.
"""
import asyncio
import math
from typing import Any, Dict, List

import pandas as pd

from backend.market_data.service import market_data_service
from shared import indicators as ta


def _clean(series: pd.Series) -> List:
    """Series → JSON-safe list with None for NaN/inf."""
    out = []
    for v in series.tolist():
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            out.append(None)
        else:
            out.append(round(float(v), 4))
    return out


def compute_indicator_signals(current: dict) -> dict:
    """Per-indicator BUY/SELL/NEUTRAL readings from the latest values."""
    signals: Dict[str, Dict[str, str]] = {}

    rsi = current.get("RSI_14")
    if rsi is not None:
        if rsi < 30:
            signals["RSI"] = {"signal": "BUY", "reason": f"RSI {rsi:.1f} — oversold"}
        elif rsi > 70:
            signals["RSI"] = {"signal": "SELL", "reason": f"RSI {rsi:.1f} — overbought"}
        else:
            signals["RSI"] = {"signal": "NEUTRAL", "reason": f"RSI {rsi:.1f} — neutral zone"}

    line, sig, hist = current.get("MACD_line"), current.get("MACD_signal"), current.get("MACD_hist")
    if None not in (line, sig, hist):
        if hist > 0 and line > sig:
            signals["MACD"] = {"signal": "BUY", "reason": "MACD above signal line"}
        elif hist < 0 and line < sig:
            signals["MACD"] = {"signal": "SELL", "reason": "MACD below signal line"}
        else:
            signals["MACD"] = {"signal": "NEUTRAL", "reason": "MACD flat"}

    close, upper, lower = current.get("close"), current.get("BB_upper"), current.get("BB_lower")
    if None not in (close, upper, lower):
        if close > upper:
            signals["BB"] = {"signal": "SELL", "reason": "Price above upper Bollinger Band"}
        elif close < lower:
            signals["BB"] = {"signal": "BUY", "reason": "Price below lower Bollinger Band"}
        else:
            signals["BB"] = {"signal": "NEUTRAL", "reason": "Price within Bollinger Bands"}

    ema20, ema50 = current.get("EMA_20"), current.get("EMA_50")
    if None not in (close, ema20, ema50):
        if close > ema20 > ema50:
            signals["EMA_Trend"] = {"signal": "BUY", "reason": "Price > EMA20 > EMA50 (uptrend)"}
        elif close < ema20 < ema50:
            signals["EMA_Trend"] = {"signal": "SELL", "reason": "Price < EMA20 < EMA50 (downtrend)"}
        else:
            signals["EMA_Trend"] = {"signal": "NEUTRAL", "reason": "Mixed EMA alignment"}

    k, d = current.get("STOCH_K"), current.get("STOCH_D")
    if None not in (k, d):
        if k < 20 and d < 20:
            signals["Stochastic"] = {"signal": "BUY", "reason": f"Stochastic {k:.1f} — oversold"}
        elif k > 80 and d > 80:
            signals["Stochastic"] = {"signal": "SELL", "reason": f"Stochastic {k:.1f} — overbought"}
        else:
            signals["Stochastic"] = {"signal": "NEUTRAL", "reason": f"Stochastic {k:.1f}"}

    return signals


def build_indicator_payload(df: pd.DataFrame, symbol: str, period: str, interval: str) -> Dict[str, Any]:
    """Pure computation (CPU-bound); run in a thread from async code."""
    full = ta.compute_all(df)
    indicators = {col: _clean(full[col]) for col in ta.INDICATOR_COLUMNS}
    current: Dict[str, Any] = {
        "close": round(float(df["close"].iloc[-1]), 4),
        "volume": int(df["volume"].iloc[-1]),
    }
    for col, values in indicators.items():
        current[col] = next((v for v in reversed(values) if v is not None), None)
    return {
        "symbol": symbol,
        "interval": interval,
        "period": period,
        "timestamps": [ts.strftime("%Y-%m-%dT%H:%M:%S") for ts in df.index],
        "ohlcv": {
            "open": _clean(df["open"]),
            "high": _clean(df["high"]),
            "low": _clean(df["low"]),
            "close": _clean(df["close"]),
            "volume": [int(v) for v in df["volume"].fillna(0)],
        },
        "indicators": indicators,
        "current": current,
        "signals": compute_indicator_signals(current),
    }


class IndicatorService:
    async def compute_all(self, symbol: str, period: str = "6mo", interval: str = "1d") -> Dict[str, Any]:
        df = await market_data_service.get_history_df(symbol, period=period, interval=interval)
        if df.empty:
            raise ValueError(f"No data for {symbol}")
        return await asyncio.to_thread(build_indicator_payload, df, symbol.upper(), period, interval)


indicator_service = IndicatorService()
