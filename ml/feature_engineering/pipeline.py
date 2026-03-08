"""
Feature Engineering Pipeline.
Generates input features for ML models from OHLCV data:
- Price-based features (returns, log returns, price ratios)
- Technical indicators (RSI, MACD, Bollinger Bands, ATR, VWAP)
- Volume features
- Volatility measures
- Momentum signals
"""
import numpy as np
import pandas as pd
try:
    import pandas_ta as ta
except ImportError:
    ta = None
from typing import Tuple


def build_features(df: pd.DataFrame, target_col: str = "close") -> pd.DataFrame:
    """
    Compute all features from an OHLCV DataFrame.
    Input df must have columns: open, high, low, close, volume
    """
    df = df.copy()
    df.columns = df.columns.str.lower()

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # ── Returns ───────────────────────────────────────────────────────────────
    df["return_1d"] = close.pct_change(1)
    df["return_5d"] = close.pct_change(5)
    df["return_10d"] = close.pct_change(10)
    df["log_return_1d"] = np.log(close / close.shift(1))

    # ── Moving Averages ───────────────────────────────────────────────────────
    for p in [5, 10, 20, 50]:
        df[f"sma_{p}"] = ta.sma(close, length=p)
        df[f"ema_{p}"] = ta.ema(close, length=p)
        df[f"price_sma_{p}_ratio"] = close / df[f"sma_{p}"]

    # ── Momentum / RSI ────────────────────────────────────────────────────────
    df["rsi_14"] = ta.rsi(close, length=14)
    df["rsi_7"] = ta.rsi(close, length=7)

    # ── MACD ──────────────────────────────────────────────────────────────────
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"] = macd.get("MACD_12_26_9")
        df["macd_signal"] = macd.get("MACDs_12_26_9")
        df["macd_hist"] = macd.get("MACDh_12_26_9")

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb = ta.bbands(close, length=20, std=2)
    if bb is not None:
        df["bb_upper"] = bb.get("BBU_20_2.0")
        df["bb_lower"] = bb.get("BBL_20_2.0")
        df["bb_bandwidth"] = bb.get("BBB_20_2.0")
        df["bb_pct"] = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

    # ── ATR & Volatility ──────────────────────────────────────────────────────
    df["atr_14"] = ta.atr(high, low, close, length=14)
    df["volatility_10d"] = close.rolling(10).std()
    df["volatility_20d"] = close.rolling(20).std()

    # ── Volume Features ───────────────────────────────────────────────────────
    df["volume_sma_20"] = volume.rolling(20).mean()
    df["volume_ratio"] = volume / df["volume_sma_20"]
    df["obv"] = ta.obv(close, volume)

    # ── Price Range ───────────────────────────────────────────────────────────
    df["daily_range"] = high - low
    df["daily_range_pct"] = df["daily_range"] / close

    # ── Candle Pattern Features ───────────────────────────────────────────────
    df["body_size"] = abs(close - df["open"])
    df["upper_shadow"] = high - pd.concat([close, df["open"]], axis=1).max(axis=1)
    df["lower_shadow"] = pd.concat([close, df["open"]], axis=1).min(axis=1) - low

    # ── Stochastic ────────────────────────────────────────────────────────────
    stoch = ta.stoch(high, low, close)
    if stoch is not None:
        df["stoch_k"] = stoch.get("STOCHk_14_3_3")
        df["stoch_d"] = stoch.get("STOCHd_14_3_3")

    # ── Williams %R ───────────────────────────────────────────────────────────
    df["willr"] = ta.willr(high, low, close, length=14)

    # ── Drop rows with NaN (from rolling windows) ─────────────────────────────
    df.dropna(inplace=True)

    return df


def create_sequences(
    data: np.ndarray,
    sequence_length: int = 60,
    target_col_idx: int = 0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Create sliding window sequences for LSTM training.
    Returns X (n_samples, seq_len, n_features) and y (n_samples,)
    """
    X, y = [], []
    for i in range(sequence_length, len(data)):
        X.append(data[i - sequence_length:i, :])
        y.append(data[i, target_col_idx])
    return np.array(X), np.array(y)
