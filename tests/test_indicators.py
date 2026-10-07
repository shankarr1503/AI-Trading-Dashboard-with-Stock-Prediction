import numpy as np
import pandas as pd
import pytest

from shared import indicators as ta
from tests.conftest import make_ohlcv


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert ta.rsi(up, 14).dropna().iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert ta.rsi(down, 14).dropna().iloc[-1] == pytest.approx(0.0, abs=1e-9)
    df = make_ohlcv(1)
    r = ta.rsi(df["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_sma_ema_known_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ta.sma(s, 5).iloc[-1] == 3.0
    assert np.isnan(ta.sma(s, 5).iloc[3])
    e = ta.ema(s, 2)
    # alpha = 2/3, seeded with the first value: 1 → 1.667 → 2.556 → 3.519 → 4.506
    assert e.iloc[-1] == pytest.approx(4.50617, rel=1e-4)


def test_atr_and_bbands_sane():
    df = make_ohlcv(2)
    atr = ta.atr(df["high"], df["low"], df["close"]).dropna()
    assert (atr > 0).all()
    bb = ta.bbands(df["close"]).dropna()
    assert (bb["upper"] >= bb["middle"]).all() and (bb["middle"] >= bb["lower"]).all()


def test_stoch_williams_ranges():
    df = make_ohlcv(3)
    st = ta.stoch(df["high"], df["low"], df["close"]).dropna()
    assert st["k"].between(0, 100).all()
    w = ta.williams_r(df["high"], df["low"], df["close"]).dropna()
    assert w.between(-100, 0).all()


def test_no_lookahead():
    """Indicator values at bar t must not change when future bars are appended."""
    df = make_ohlcv(4, n=400)
    full = ta.compute_all(df)
    partial = ta.compute_all(df.iloc[:300])
    cols = [c for c in ta.INDICATOR_COLUMNS if c != "VWAP"]
    pd.testing.assert_frame_equal(full[cols].iloc[:300], partial[cols], check_exact=False, rtol=1e-9)


def test_daily_vwap_is_rolling():
    df = make_ohlcv(5, n=60)
    v = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    assert v.iloc[:19].isna().all() and v.iloc[19:].notna().all()
