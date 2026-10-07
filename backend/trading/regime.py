"""Market regime classification from trend strength and volatility."""
from __future__ import annotations

import math
from enum import Enum

import pandas as pd


class Regime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


def _val(row: pd.Series, key: str) -> float | None:
    v = row.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def volatility_percentile(rvol: pd.Series, lookback: int = 252) -> float | None:
    """Percentile rank of the latest realised volatility within its trailing window."""
    window = rvol.dropna().iloc[-lookback:]
    if len(window) < 60:
        return None
    return float((window <= window.iloc[-1]).mean())


def detect_regime(ind: pd.DataFrame, adx_threshold: float = 22.0, high_vol_pct: float = 0.90) -> Regime:
    """
    Classify the latest bar of an indicator frame (output of shared.indicators.compute_all).

    HIGH_VOLATILITY wins when realised vol is in the top decile of its past year:
    signals are least reliable and gaps most likely, so the risk layer shrinks size.
    """
    row = ind.iloc[-1]
    vol_pct = volatility_percentile(ind["RVOL_20"]) if "RVOL_20" in ind else None
    if vol_pct is not None and vol_pct >= high_vol_pct:
        return Regime.HIGH_VOLATILITY

    close = _val(row, "close")
    ema50 = _val(row, "EMA_50")
    ema200 = _val(row, "EMA_200") or _val(row, "SMA_200")
    adx = _val(row, "ADX_14")
    if None in (close, ema50, adx):
        return Regime.RANGE

    long_ma = ema200 if ema200 is not None else ema50
    if adx >= adx_threshold:
        if close > ema50 and ema50 >= long_ma:
            return Regime.BULL_TREND
        if close < ema50 and ema50 <= long_ma:
            return Regime.BEAR_TREND
    return Regime.RANGE
