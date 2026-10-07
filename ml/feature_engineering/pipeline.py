"""
Feature Engineering Pipeline.

Builds stationary, scale-free features from OHLCV data (returns, ratios to
moving averages, oscillators, volatility, volume ratios) using the shared
pure-pandas indicator library. Models predict the *next-day log return*
(`target`), not the price level: returns are closer to stationary, so tree
models aren't asked to extrapolate beyond the price range they were trained on.
"""
from typing import List, Tuple

import numpy as np
import pandas as pd

from shared import indicators as ta

FEATURE_COLUMNS: List[str] = [
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "price_sma10", "price_sma20", "price_sma50", "ema10_ema20",
    "rsi_14", "rsi_7", "macd_norm", "macd_signal_norm", "macd_hist_norm",
    "bb_pct", "bb_bandwidth", "atr_pct", "vol_10d", "vol_20d",
    "volume_ratio", "obv_flow_20", "range_pct", "body_pct", "upper_shadow_pct", "lower_shadow_pct",
    "stoch_k", "stoch_d", "willr", "adx",
]


def build_features(df: pd.DataFrame, dropna: bool = True) -> pd.DataFrame:
    """
    Compute features from an OHLCV DataFrame with columns open/high/low/close/volume.
    Adds `target` = next-day log return (NaN on the last row, which is the row we predict from).
    """
    df = df.copy()
    df.columns = df.columns.str.lower()
    o, h, l, c, v = (df[k].astype(float) for k in ("open", "high", "low", "close", "volume"))

    log_c = np.log(c)
    for n in (1, 5, 10, 20):
        df[f"ret_{n}d"] = log_c.diff(n)
    df["price_sma10"] = c / ta.sma(c, 10) - 1
    df["price_sma20"] = c / ta.sma(c, 20) - 1
    df["price_sma50"] = c / ta.sma(c, 50) - 1
    df["ema10_ema20"] = ta.ema(c, 10) / ta.ema(c, 20) - 1

    df["rsi_14"] = ta.rsi(c, 14) / 100
    df["rsi_7"] = ta.rsi(c, 7) / 100
    m = ta.macd(c)
    df["macd_norm"] = m["macd"] / c
    df["macd_signal_norm"] = m["signal"] / c
    df["macd_hist_norm"] = m["hist"] / c

    bb = ta.bbands(c)
    df["bb_pct"] = bb["pct_b"]
    df["bb_bandwidth"] = bb["bandwidth"] / 100
    df["atr_pct"] = ta.atr(h, l, c, 14) / c
    df["vol_10d"] = df["ret_1d"].rolling(10).std()
    df["vol_20d"] = df["ret_1d"].rolling(20).std()

    df["volume_ratio"] = v / v.rolling(20).mean()
    vol_sum = v.rolling(20).sum().replace(0, np.nan)
    obv = ta.obv(c, v)
    df["obv_flow_20"] = (obv - obv.shift(20)) / vol_sum

    df["range_pct"] = (h - l) / c
    df["body_pct"] = (c - o) / c
    df["upper_shadow_pct"] = (h - pd.concat([c, o], axis=1).max(axis=1)) / c
    df["lower_shadow_pct"] = (pd.concat([c, o], axis=1).min(axis=1) - l) / c

    st = ta.stoch(h, l, c)
    df["stoch_k"] = st["k"] / 100
    df["stoch_d"] = st["d"] / 100
    df["willr"] = ta.williams_r(h, l, c, 14) / 100
    df["adx"] = ta.adx(h, l, c, 14)["adx"] / 100

    df["target"] = log_c.shift(-1) - log_c
    df = df.replace([np.inf, -np.inf], np.nan)
    if dropna:
        df = df.dropna(subset=FEATURE_COLUMNS)
    return df


def train_test_split_time(df: pd.DataFrame, test_frac: float = 0.2) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split (never shuffle time series)."""
    labelled = df.dropna(subset=["target"])
    split = int(len(labelled) * (1 - test_frac))
    return labelled.iloc[:split], labelled.iloc[split:]


def create_sequences(data: np.ndarray, targets: np.ndarray, sequence_length: int = 60) -> Tuple[np.ndarray, np.ndarray]:
    """Sliding windows: X[i] = data[i-seq:i], y[i] = targets[i-1] (the return following the window's last bar)."""
    X, y = [], []
    for i in range(sequence_length, len(data) + 1):
        X.append(data[i - sequence_length:i])
        y.append(targets[i - 1])
    return np.asarray(X), np.asarray(y)
