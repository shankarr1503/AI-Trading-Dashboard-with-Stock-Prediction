"""
Technical indicators implemented with plain pandas/numpy.

Replaces the unmaintained `pandas-ta` dependency (which is broken on numpy 2 and
was never actually installed by the backend). Formulas follow the conventional
definitions (Wilder smoothing for RSI/ATR/ADX), so values match TradingView /
pandas-ta within floating point tolerance.

Every function takes pandas Series aligned on the same index and returns Series
(or a DataFrame) on that index. Warm-up periods are NaN — never back-filled — so
no indicator value ever depends on future bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    """Element-wise division that yields NaN instead of inf when the denominator is 0."""
    return num / den.replace(0, np.nan)


# ─── Moving averages ──────────────────────────────────────────────────────────

def sma(close: pd.Series, length: int) -> pd.Series:
    return close.rolling(length, min_periods=length).mean()


def ema(close: pd.Series, length: int) -> pd.Series:
    return close.ewm(span=length, adjust=False, min_periods=length).mean()


def _wilder(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing (an EMA with alpha = 1/length)."""
    return series.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


# ─── Momentum ─────────────────────────────────────────────────────────────────

def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), length)
    loss = _wilder(-delta.clip(upper=0), length)
    rs = _safe_div(gain, loss)
    out = 100 - 100 / (1 + rs)
    # No losses in the window → RSI is 100 by definition.
    out = out.where(~((loss == 0) & gain.notna()), 100.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig}, index=close.index)


def momentum(close: pd.Series, length: int = 10) -> pd.Series:
    return close - close.shift(length)


def stoch(high: pd.Series, low: pd.Series, close: pd.Series,
          k: int = 14, d: int = 3, smooth_k: int = 3) -> pd.DataFrame:
    lowest = low.rolling(k, min_periods=k).min()
    highest = high.rolling(k, min_periods=k).max()
    raw_k = 100 * _safe_div(close - lowest, highest - lowest)
    k_line = raw_k.rolling(smooth_k, min_periods=smooth_k).mean()
    d_line = k_line.rolling(d, min_periods=d).mean()
    return pd.DataFrame({"k": k_line, "d": d_line}, index=close.index)


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    highest = high.rolling(length, min_periods=length).max()
    lowest = low.rolling(length, min_periods=length).min()
    return -100 * _safe_div(highest - close, highest - lowest)


# ─── Volatility ───────────────────────────────────────────────────────────────

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1, skipna=False)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    tr.iloc[0] = high.iloc[0] - low.iloc[0] if len(tr) else np.nan
    return _wilder(tr, length)


def bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, length)
    dev = close.rolling(length, min_periods=length).std(ddof=0)
    upper = mid + std * dev
    lower = mid - std * dev
    return pd.DataFrame(
        {
            "upper": upper,
            "middle": mid,
            "lower": lower,
            "bandwidth": 100 * _safe_div(upper - lower, mid),
            "pct_b": _safe_div(close - lower, upper - lower),
        },
        index=close.index,
    )


def realized_volatility(close: pd.Series, length: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Annualised close-to-close volatility of log returns."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(length, min_periods=length).std() * np.sqrt(periods_per_year)


# ─── Trend strength ───────────────────────────────────────────────────────────

def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.DataFrame:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)
    atr_ = atr(high, low, close, length)
    plus_di = 100 * _safe_div(_wilder(plus_dm, length), atr_)
    minus_di = 100 * _safe_div(_wilder(minus_dm, length), atr_)
    dx = 100 * _safe_div((plus_di - minus_di).abs(), plus_di + minus_di)
    return pd.DataFrame(
        {"adx": _wilder(dx, length), "plus_di": plus_di, "minus_di": minus_di}, index=close.index
    )


# ─── Volume ───────────────────────────────────────────────────────────────────

def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
         length: int | None = None) -> pd.Series:
    """
    Volume-weighted average price.

    Intraday data (multiple bars per day) is anchored to each session, like a
    broker's VWAP. Daily or slower data has no meaningful session anchor, so a
    rolling `length`-bar VWAP is returned instead (default 20).
    """
    typical = (high + low + close) / 3
    pv = typical * volume
    idx = close.index
    if isinstance(idx, pd.DatetimeIndex) and length is None and len(idx) > 1:
        sessions = idx.normalize()
        if sessions.duplicated().any():
            cum_pv = pv.groupby(sessions).cumsum()
            cum_v = volume.groupby(sessions).cumsum()
            return _safe_div(cum_pv, cum_v)
    window = length or 20
    return _safe_div(
        pv.rolling(window, min_periods=window).sum(),
        volume.rolling(window, min_periods=window).sum(),
    )


# ─── Convenience bundle ───────────────────────────────────────────────────────

def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute the standard indicator set for an OHLCV frame with lower-case
    columns open/high/low/close/volume. Returns a new frame (input untouched)
    containing the original columns plus indicator columns.
    """
    out = df.copy()
    o, h, l, c, v = (out[col].astype(float) for col in ("open", "high", "low", "close", "volume"))

    for n in (10, 20, 50, 200):
        out[f"SMA_{n}"] = sma(c, n)
        out[f"EMA_{n}"] = ema(c, n)

    out["RSI_14"] = rsi(c, 14)
    m = macd(c)
    out["MACD_line"], out["MACD_signal"], out["MACD_hist"] = m["macd"], m["signal"], m["hist"]
    bb = bbands(c)
    out["BB_upper"], out["BB_middle"], out["BB_lower"] = bb["upper"], bb["middle"], bb["lower"]
    out["BB_bandwidth"], out["BB_pct"] = bb["bandwidth"], bb["pct_b"]
    st = stoch(h, l, c)
    out["STOCH_K"], out["STOCH_D"] = st["k"], st["d"]
    out["ATR_14"] = atr(h, l, c, 14)
    out["VWAP"] = vwap(h, l, c, v)
    out["MOMENTUM_10"] = momentum(c, 10)
    out["WILLIAMS_R"] = williams_r(h, l, c, 14)
    a = adx(h, l, c, 14)
    out["ADX_14"], out["PLUS_DI"], out["MINUS_DI"] = a["adx"], a["plus_di"], a["minus_di"]
    out["OBV"] = obv(c, v)
    out["RVOL_20"] = realized_volatility(c, 20)
    return out


INDICATOR_COLUMNS = [
    "SMA_10", "EMA_10", "SMA_20", "EMA_20", "SMA_50", "EMA_50", "SMA_200", "EMA_200",
    "RSI_14", "MACD_line", "MACD_signal", "MACD_hist",
    "BB_upper", "BB_middle", "BB_lower", "BB_bandwidth", "BB_pct",
    "STOCH_K", "STOCH_D", "ATR_14", "VWAP", "MOMENTUM_10", "WILLIAMS_R",
    "ADX_14", "PLUS_DI", "MINUS_DI", "OBV", "RVOL_20",
]
