"""
Technical Indicator Engine.
Computes SMA, EMA, RSI, MACD, Bollinger Bands, VWAP, ATR, Stochastic, Momentum.
Uses pandas-ta for computation on historical OHLCV DataFrames.
"""
import logging
from typing import Dict, Any, List
import pandas as pd
try:
    import pandas_ta as ta
except ImportError:
    ta = None
    import logging
    logging.getLogger(__name__).warning("pandas_ta not installed. Technical indicators will be limited.")
import numpy as np

from backend.market_data.service import market_data_service

logger = logging.getLogger(__name__)


class IndicatorService:
    """Computes all technical indicators from OHLCV data."""

    async def compute_all(
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1d",
    ) -> Dict[str, Any]:
        """
        Fetch OHLCV data and compute all technical indicators.
        Returns a dict of indicator arrays aligned with the price data.
        """
        raw = await market_data_service.get_history(symbol, period=period, interval=interval)
        if not raw:
            raise ValueError(f"No data for {symbol}")

        df = pd.DataFrame(raw)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)

        result = {
            "symbol": symbol.upper(),
            "interval": interval,
            "period": period,
            "timestamps": df.index.strftime("%Y-%m-%dT%H:%M:%S").tolist(),
            "ohlcv": {
                "open": df["open"].round(4).tolist(),
                "high": df["high"].round(4).tolist(),
                "low": df["low"].round(4).tolist(),
                "close": df["close"].round(4).tolist(),
                "volume": df["volume"].astype(int).tolist(),
            },
            "indicators": {},
        }

        ind = result["indicators"]

        # ── Moving Averages ────────────────────────────────────────────────────
        for period_val in [10, 20, 50, 200]:
            sma = ta.sma(df["close"], length=period_val)
            ema = ta.ema(df["close"], length=period_val)
            if sma is not None:
                ind[f"SMA_{period_val}"] = self._clean(sma)
            if ema is not None:
                ind[f"EMA_{period_val}"] = self._clean(ema)

        # ── RSI ───────────────────────────────────────────────────────────────
        rsi = ta.rsi(df["close"], length=14)
        if rsi is not None:
            ind["RSI_14"] = self._clean(rsi)

        # ── MACD ──────────────────────────────────────────────────────────────
        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is not None:
            ind["MACD_line"] = self._clean(macd.get("MACD_12_26_9"))
            ind["MACD_signal"] = self._clean(macd.get("MACDs_12_26_9"))
            ind["MACD_hist"] = self._clean(macd.get("MACDh_12_26_9"))

        # ── Bollinger Bands ────────────────────────────────────────────────────
        bb = ta.bbands(df["close"], length=20, std=2)
        if bb is not None:
            ind["BB_upper"] = self._clean(bb.get("BBU_20_2.0"))
            ind["BB_middle"] = self._clean(bb.get("BBM_20_2.0"))
            ind["BB_lower"] = self._clean(bb.get("BBL_20_2.0"))
            ind["BB_bandwidth"] = self._clean(bb.get("BBB_20_2.0"))

        # ── Stochastic Oscillator ─────────────────────────────────────────────
        stoch = ta.stoch(df["high"], df["low"], df["close"])
        if stoch is not None:
            ind["STOCH_K"] = self._clean(stoch.get("STOCHk_14_3_3"))
            ind["STOCH_D"] = self._clean(stoch.get("STOCHd_14_3_3"))

        # ── ATR ───────────────────────────────────────────────────────────────
        atr = ta.atr(df["high"], df["low"], df["close"], length=14)
        if atr is not None:
            ind["ATR_14"] = self._clean(atr)

        # ── VWAP ──────────────────────────────────────────────────────────────
        try:
            vwap = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
            if vwap is not None:
                ind["VWAP"] = self._clean(vwap)
        except Exception:
            pass  # VWAP not always computable (daily data)

        # ── Momentum ──────────────────────────────────────────────────────────
        mom = ta.mom(df["close"], length=10)
        if mom is not None:
            ind["MOMENTUM_10"] = self._clean(mom)

        # ── Williams %R ───────────────────────────────────────────────────────
        willr = ta.willr(df["high"], df["low"], df["close"], length=14)
        if willr is not None:
            ind["WILLIAMS_R"] = self._clean(willr)

        # ── Summary / Current Values ──────────────────────────────────────────
        result["current"] = self._get_current_values(df, ind)
        result["signals"] = self._compute_indicator_signals(result["current"])

        return result

    def _clean(self, series) -> List:
        """Convert pandas Series to Python list with None for NaN."""
        if series is None:
            return []
        return [None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(v, 4)
                for v in series]

    def _get_current_values(self, df: pd.DataFrame, ind: dict) -> dict:
        """Extract the latest value for each indicator."""
        current = {
            "close": round(float(df["close"].iloc[-1]), 4),
            "volume": int(df["volume"].iloc[-1]),
        }
        for key, values in ind.items():
            last = next((v for v in reversed(values) if v is not None), None)
            current[key] = last
        return current

    def _compute_indicator_signals(self, current: dict) -> dict:
        """Generate simple BUY/SELL/NEUTRAL signals from each indicator."""
        signals = {}

        # RSI signal
        rsi = current.get("RSI_14")
        if rsi is not None:
            if rsi < 30:
                signals["RSI"] = {"signal": "BUY", "reason": f"RSI {rsi:.1f} — Oversold"}
            elif rsi > 70:
                signals["RSI"] = {"signal": "SELL", "reason": f"RSI {rsi:.1f} — Overbought"}
            else:
                signals["RSI"] = {"signal": "NEUTRAL", "reason": f"RSI {rsi:.1f} — Neutral zone"}

        # MACD signal
        macd_line = current.get("MACD_line")
        macd_signal = current.get("MACD_signal")
        macd_hist = current.get("MACD_hist")
        if all(v is not None for v in [macd_line, macd_signal, macd_hist]):
            if macd_hist > 0 and macd_line > macd_signal:
                signals["MACD"] = {"signal": "BUY", "reason": "MACD bullish crossover"}
            elif macd_hist < 0 and macd_line < macd_signal:
                signals["MACD"] = {"signal": "SELL", "reason": "MACD bearish crossover"}
            else:
                signals["MACD"] = {"signal": "NEUTRAL", "reason": "MACD no clear signal"}

        # Bollinger Bands signal
        close = current.get("close")
        bb_upper = current.get("BB_upper")
        bb_lower = current.get("BB_lower")
        if all(v is not None for v in [close, bb_upper, bb_lower]):
            if close > bb_upper:
                signals["BB"] = {"signal": "SELL", "reason": "Price above upper Bollinger Band"}
            elif close < bb_lower:
                signals["BB"] = {"signal": "BUY", "reason": "Price below lower Bollinger Band"}
            else:
                signals["BB"] = {"signal": "NEUTRAL", "reason": "Price within Bollinger Bands"}

        # EMA trend signal
        ema20 = current.get("EMA_20")
        ema50 = current.get("EMA_50")
        if all(v is not None for v in [close, ema20, ema50]):
            if close > ema20 > ema50:
                signals["EMA_Trend"] = {"signal": "BUY", "reason": "Price > EMA20 > EMA50 (uptrend)"}
            elif close < ema20 < ema50:
                signals["EMA_Trend"] = {"signal": "SELL", "reason": "Price < EMA20 < EMA50 (downtrend)"}
            else:
                signals["EMA_Trend"] = {"signal": "NEUTRAL", "reason": "Mixed EMA signals"}

        # Stochastic signal
        stoch_k = current.get("STOCH_K")
        stoch_d = current.get("STOCH_D")
        if all(v is not None for v in [stoch_k, stoch_d]):
            if stoch_k < 20 and stoch_d < 20:
                signals["Stochastic"] = {"signal": "BUY", "reason": f"Stochastic {stoch_k:.1f} — Oversold"}
            elif stoch_k > 80 and stoch_d > 80:
                signals["Stochastic"] = {"signal": "SELL", "reason": f"Stochastic {stoch_k:.1f} — Overbought"}
            else:
                signals["Stochastic"] = {"signal": "NEUTRAL", "reason": f"Stochastic {stoch_k:.1f}"}

        return signals


indicator_service = IndicatorService()
