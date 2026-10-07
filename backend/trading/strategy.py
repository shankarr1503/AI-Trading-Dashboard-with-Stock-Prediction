"""
Regime-adaptive multi-factor strategy.

Six independent evidence sources, each scored in [-1, 1]:

  trend       EMA 20/50/200 alignment + MACD histogram, scaled by ADX strength
  momentum    volatility-normalised 3-month return (skipping the last week) and 1-month return
  meanrev     regime-aware: oversold/overbought in ranges, pullback entries in trends
  volume      on-balance-volume flow over 20 bars (is volume confirming price?)
  ml          ML ensemble P(up) from the ML service (live trading only)
  sentiment   news sentiment (live trading only)

Weights depend on the detected regime (trend factors dominate in trends,
mean reversion dominates in ranges). The composite score drives entries;
stops/targets come from ATR. All computations are causal (bar t only uses
data up to t), so the same code drives the backtester and live trading.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from backend.trading.regime import Regime
from shared import indicators as ta


@dataclass
class StrategyConfig:
    entry_threshold: float = 0.25
    bear_entry_penalty: float = 0.20     # extra score required to buy in a bear trend
    exit_threshold: float = -0.10
    stop_atr_mult: float = 2.0
    high_vol_stop_mult: float = 1.25     # widen stops in high-volatility regimes
    target_atr_mult: float = 4.0
    trail_atr_mult: float = 3.0
    breakeven_after_atr: float = 1.5     # move stop to breakeven after +1.5 ATR
    max_holding_bars: int = 40
    # Skip an entry if price has moved more than this many ATRs away from the
    # signal bar's close by the time the order would execute.
    gap_filter_atr: float = 2.0
    min_history: int = 210
    adx_threshold: float = 22.0
    high_vol_percentile: float = 0.90

    def to_dict(self) -> dict:
        return asdict(self)


# Technical weights drive the validated (backtested) tech_score. The ml,
# sentiment and fundamental overlays only enter the blended `score`, which the
# live bot uses solely to *block* trades, never to create them.
REGIME_WEIGHTS: Dict[Regime, Dict[str, float]] = {
    Regime.BULL_TREND: {"trend": 0.35, "momentum": 0.25, "meanrev": 0.10, "volume": 0.10, "ml": 0.15, "sentiment": 0.05, "fundamental": 0.10},
    Regime.BEAR_TREND: {"trend": 0.35, "momentum": 0.25, "meanrev": 0.10, "volume": 0.10, "ml": 0.15, "sentiment": 0.05, "fundamental": 0.10},
    Regime.RANGE: {"trend": 0.10, "momentum": 0.10, "meanrev": 0.40, "volume": 0.10, "ml": 0.20, "sentiment": 0.10, "fundamental": 0.10},
    Regime.HIGH_VOLATILITY: {"trend": 0.20, "momentum": 0.15, "meanrev": 0.25, "volume": 0.10, "ml": 0.20, "sentiment": 0.10, "fundamental": 0.10},
}
TECH_FACTORS = ("trend", "momentum", "meanrev", "volume")


def _clip(x, lo=-1.0, hi=1.0):
    return np.clip(x, lo, hi)


def compute_regimes(ind: pd.DataFrame, cfg: StrategyConfig) -> pd.Series:
    """Vectorised regime classification (same rules as regime.detect_regime)."""
    vol_rank = ind["RVOL_20"].rolling(252, min_periods=60).rank(pct=True)
    long_ma = ind["EMA_200"].fillna(ind["EMA_50"])
    trending = ind["ADX_14"] >= cfg.adx_threshold
    bull = trending & (ind["close"] > ind["EMA_50"]) & (ind["EMA_50"] >= long_ma)
    bear = trending & (ind["close"] < ind["EMA_50"]) & (ind["EMA_50"] <= long_ma)
    regime = pd.Series(Regime.RANGE.value, index=ind.index, dtype=object)
    regime[bull] = Regime.BULL_TREND.value
    regime[bear] = Regime.BEAR_TREND.value
    regime[vol_rank >= cfg.high_vol_percentile] = Regime.HIGH_VOLATILITY.value
    return regime


def compute_factor_frame(ohlcv: pd.DataFrame, cfg: Optional[StrategyConfig] = None) -> pd.DataFrame:
    """
    Indicators + regime + technical factor scores for every bar.
    Input: OHLCV frame with lower-case columns. Output adds indicator columns,
    `regime`, the four technical factor columns and `tech_score`.
    """
    cfg = cfg or StrategyConfig()
    ind = ta.compute_all(ohlcv)
    c = ind["close"]
    atr = ind["ATR_14"]

    # ── Trend ──
    align = (
        np.sign(c - ind["EMA_50"])
        + np.sign(ind["EMA_20"] - ind["EMA_50"])
        + np.sign(ind["EMA_50"] - ind["EMA_200"].fillna(ind["EMA_50"]))
    ) / 3.0
    macd_term = np.tanh(ind["MACD_hist"] / (0.2 * atr))
    strength = _clip((ind["ADX_14"] - 15) / 20, 0, 1)
    ind["trend"] = (0.7 * align + 0.3 * macd_term) * (0.5 + 0.5 * strength)

    # ── Momentum (vol-normalised; skip most recent week to avoid short-term reversal) ──
    daily_vol = (ind["RVOL_20"] / math.sqrt(252)).replace(0, np.nan)
    r_63 = c.shift(5) / c.shift(68) - 1
    r_21 = c / c.shift(21) - 1
    ind["momentum"] = 0.7 * np.tanh(r_63 / (daily_vol * math.sqrt(63))) + 0.3 * np.tanh(r_21 / (daily_vol * math.sqrt(21)))

    # ── Mean reversion (regime-aware) ──
    ind["regime"] = compute_regimes(ind, cfg)
    rsi = ind["RSI_14"]
    range_mr = 0.5 * _clip((0.5 - ind["BB_pct"]) * 2) + 0.5 * _clip((50 - rsi) / 25)
    above_200 = c > ind["EMA_200"].fillna(ind["SMA_200"]).fillna(ind["EMA_50"])
    bull_mr = np.where((rsi < 45) & above_200, _clip((45 - rsi) / 20, 0, 1),
                       np.where(rsi > 78, -_clip((rsi - 78) / 15, 0, 1), 0.0))
    bear_mr = np.where((rsi > 55) & ~above_200, -_clip((rsi - 55) / 20, 0, 1),
                       np.where(rsi < 22, _clip((22 - rsi) / 15, 0, 1), 0.0))
    regime = ind["regime"]
    ind["meanrev"] = np.select(
        [regime == Regime.BULL_TREND.value, regime == Regime.BEAR_TREND.value, regime == Regime.HIGH_VOLATILITY.value],
        [bull_mr, bear_mr, 0.5 * range_mr],
        default=range_mr,
    )

    # ── Volume flow: net OBV change over 20 bars / total volume over 20 bars ∈ [-1, 1] ──
    vol_sum = ind["volume"].rolling(20, min_periods=20).sum().replace(0, np.nan)
    ind["volume_flow"] = _clip((ind["OBV"] - ind["OBV"].shift(20)) / vol_sum)

    ind["tech_score"] = combine_scores(ind, regime)
    return ind


def _weights_frame(regime: pd.Series, factors) -> pd.DataFrame:
    rows = {r.value: REGIME_WEIGHTS[r] for r in Regime}
    w = pd.DataFrame([rows[r] for r in regime], index=regime.index)
    return w[list(factors)]


def combine_scores(ind: pd.DataFrame, regime: pd.Series) -> pd.Series:
    """Regime-weighted average of the technical factors (weights renormalised)."""
    factors = {"trend": ind["trend"], "momentum": ind["momentum"], "meanrev": ind["meanrev"], "volume": ind["volume_flow"]}
    w = _weights_frame(regime, factors.keys())
    vals = pd.DataFrame(factors)
    mask = vals.notna()
    num = (vals.fillna(0) * w).sum(axis=1)
    den = (w * mask).sum(axis=1).replace(0, np.nan)
    return _clip(num / den)


@dataclass
class Analysis:
    symbol: str
    timestamp: str
    price: float
    atr: float
    atr_pct: float
    regime: str
    score: float
    tech_score: float
    components: Dict[str, Dict[str, Any]]
    signal: str               # BUY / SELL / HOLD
    stop: float
    target: float
    reward_risk: float
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def ml_factor(ml: Optional[dict]) -> Optional[float]:
    """Map an ML prediction payload to [-1, 1]; ignores the statistical fallback."""
    if not ml or ml.get("source") != "ml_service":
        return None
    nd = (ml.get("predictions") or {}).get("next_day") or {}
    p_up = nd.get("bullish_probability")
    if p_up is None:
        return None
    return float(_clip((float(p_up) - 0.5) * 4))


def fundamental_factor(view: Optional[dict]) -> Optional[float]:
    """Research scorecard composite (0–100) → [-1, 1]; ignored when coverage is thin."""
    if not view or not view.get("available") or view.get("composite") is None or (view.get("coverage") or 0) < 0.5:
        return None
    return float(_clip((float(view["composite"]) - 50) / 50))


def sentiment_factor(sentiment: Optional[dict]) -> Optional[float]:
    if not sentiment or sentiment.get("headlines_analyzed", 0) < 3:
        return None
    return float(_clip(float(sentiment.get("compound_score", 0.0)) * 2))


def stop_and_target(price: float, atr: float, regime: str, cfg: StrategyConfig) -> tuple[float, float]:
    stop_mult = cfg.stop_atr_mult * (cfg.high_vol_stop_mult if regime == Regime.HIGH_VOLATILITY.value else 1.0)
    return price - stop_mult * atr, price + cfg.target_atr_mult * atr


def analyze_latest(
    symbol: str,
    frame: pd.DataFrame,
    cfg: Optional[StrategyConfig] = None,
    ml: Optional[dict] = None,
    sentiment: Optional[dict] = None,
    fundamental: Optional[dict] = None,
) -> Analysis:
    """
    Analysis of the latest bar. `tech_score` is the backtested technical signal;
    `score` additionally blends the ML, sentiment and fundamental overlays.
    """
    cfg = cfg or StrategyConfig()
    if len(frame) < cfg.min_history:
        raise ValueError(f"{symbol}: need {cfg.min_history} bars of history, have {len(frame)}")
    row = frame.iloc[-1]
    regime = Regime(row["regime"])
    price = float(row["close"])
    atr = float(row["ATR_14"])
    if not (atr > 0) or not (price > 0):
        raise ValueError(f"{symbol}: invalid price/ATR")

    raw = {
        "trend": row["trend"], "momentum": row["momentum"],
        "meanrev": row["meanrev"], "volume": row["volume_flow"],
        "ml": ml_factor(ml), "sentiment": sentiment_factor(sentiment),
        "fundamental": fundamental_factor(fundamental),
    }
    weights = REGIME_WEIGHTS[regime]
    components: Dict[str, Dict[str, Any]] = {}
    num = den = 0.0
    for name, val in raw.items():
        ok = val is not None and not (isinstance(val, float) and math.isnan(val))
        components[name] = {"score": round(float(val), 4) if ok else None, "weight": weights[name] if ok else 0.0}
        if ok:
            num += weights[name] * float(val)
            den += weights[name]
    score = float(_clip(num / den)) if den else 0.0

    stop, target = stop_and_target(price, atr, regime.value, cfg)
    reward_risk = (target - price) / (price - stop)

    tech = float(row["tech_score"]) if not math.isnan(row["tech_score"]) else 0.0
    threshold = cfg.entry_threshold + (cfg.bear_entry_penalty if regime == Regime.BEAR_TREND else 0.0)
    # BUY needs the validated technical signal AND no overlay veto.
    if tech >= threshold and score >= threshold:
        signal = "BUY"
    elif tech <= cfg.exit_threshold:
        signal = "SELL"
    else:
        signal = "HOLD"

    reasons = [f"Regime {regime.value}; technical score {tech:+.2f}, blended {score:+.2f} (entry ≥ {threshold:.2f})"]
    for name, comp in components.items():
        if comp["score"] is not None:
            reasons.append(f"{name}: {comp['score']:+.2f} × w{comp['weight']:.2f}")

    return Analysis(
        symbol=symbol,
        timestamp=str(frame.index[-1]),
        price=round(price, 4),
        atr=round(atr, 4),
        atr_pct=round(atr / price, 5),
        regime=regime.value,
        score=round(score, 4),
        tech_score=round(tech, 4),
        components=components,
        signal=signal,
        stop=round(stop, 4),
        target=round(target, 4),
        reward_risk=round(reward_risk, 3),
        reasons=reasons,
    )


def update_trailing_stop(
    stop: float, entry_price: float, highest: float, close: float, atr: float,
    entry_atr: float, round_trip_cost_pct: float, cfg: StrategyConfig,
) -> tuple[float, float]:
    """
    Ratchet the stop upward (never down). Returns (new_stop, new_highest).
    1) After +breakeven_after_atr × ATR in profit, lock in breakeven *including costs*.
    2) Chandelier trail: highest close − trail_atr_mult × ATR.
    """
    highest = max(highest, close)
    new_stop = stop
    if highest >= entry_price + cfg.breakeven_after_atr * entry_atr:
        new_stop = max(new_stop, entry_price * (1 + round_trip_cost_pct))
    if atr > 0:
        new_stop = max(new_stop, highest - cfg.trail_atr_mult * atr)
    return new_stop, highest


def exit_signal(score: float, bars_held: int, cfg: StrategyConfig) -> Optional[str]:
    if score <= cfg.exit_threshold:
        return "signal_reversal"
    if bars_held >= cfg.max_holding_bars:
        return "time_stop"
    return None
