"""
Multi-factor scorecard (0–100 per factor) in the style of a quant equity desk:
Value, Quality, Growth, Momentum, Low Risk and Street sentiment.

Scores are *absolute*: each metric is mapped onto 0–100 with fixed, documented
breakpoints, so a single-stock report and a universe screen use the same
yardstick. The screener adds cross-sectional percentile ranks on top.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

FACTOR_WEIGHTS = {"value": 0.20, "quality": 0.25, "growth": 0.15, "momentum": 0.20, "low_risk": 0.10, "street": 0.10}

# Metrics that are meaningless for one kind of company are excluded, not scored 0.
# (Metrics that are merely *unavailable* — e.g. price multiples when the
# statements and the price are in different currencies — stay in the
# denominator unscored, so coverage drops honestly.)
FINANCIALS_ONLY = {"price_to_book", "roe"}
NOT_FOR_FINANCIALS = {"fcf_yield", "ev_ebitda", "gross_margin", "interest_coverage", "net_debt_to_ebitda",
                      "cash_conversion", "fcf_cagr"}


def lin(x: Optional[float], worst: float, best: float) -> Optional[float]:
    """Linear map: `worst` → 0, `best` → 100 (works for reversed ranges), clipped."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    if best == worst:
        return None
    return float(np.clip((x - worst) / (best - worst) * 100, 0, 100))


def price_stats(df: Optional[pd.DataFrame]) -> Dict[str, Optional[float]]:
    """Momentum and risk statistics from ~1–2 years of daily closes."""
    if df is None or len(df) < 60:
        return {}
    c = df["close"].astype(float)
    rets = c.pct_change().dropna()
    last = c.iloc[-1]

    def ret_between(start_bars: int, end_bars: int) -> Optional[float]:
        if len(c) <= start_bars:
            return None
        return float(c.iloc[-1 - end_bars] / c.iloc[-1 - start_bars] - 1)

    year = c.iloc[-252:]
    ma200 = c.rolling(200).mean().iloc[-1] if len(c) >= 200 else None
    return {
        "return_12_1m": ret_between(252, 21),
        "return_6m": ret_between(126, 0),
        "return_1m": ret_between(21, 0),
        "price_vs_ma200": float(last / ma200 - 1) if ma200 else None,
        "volatility_1y": float(rets.iloc[-252:].std() * math.sqrt(252)),
        "max_drawdown_1y": float((year / year.cummax() - 1).min()),
    }


def _avg_surprise(street: Dict[str, Any]) -> Optional[float]:
    vals = []
    for rec in street.get("earnings_history") or []:
        v = rec.get("surprisePercent")
        if isinstance(v, (int, float)) and abs(v) < 5:
            vals.append(float(v))
    return sum(vals) / len(vals) if vals else None


def score(fundamentals: Dict[str, Any], valuation: Dict[str, Any], snapshot: Dict[str, Any],
          prices: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Any]:
    m = fundamentals.get("metrics", {}) if fundamentals.get("available") else {}
    mult = valuation.get("multiples", {})
    info = snapshot.get("info_metrics", {})
    street = snapshot.get("street", {})
    prices = prices or {}
    is_fin = fundamentals.get("is_financial", False)
    piotroski = (fundamentals.get("piotroski") or {}).get("score")

    subs: Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]] = {
        "value": {
            "earnings_yield": (mult.get("earnings_yield"), lin(mult.get("earnings_yield"), 0.0, 0.08)),
            "fcf_yield": (mult.get("fcf_yield"), lin(mult.get("fcf_yield"), 0.0, 0.08)),
            "ev_ebitda": (mult.get("ev_ebitda"), lin(mult.get("ev_ebitda"), 25, 6)),
            "price_to_book": (mult.get("price_to_book"), lin(mult.get("price_to_book"), 3.0, 0.8)),
            "intrinsic_upside_pct": (valuation.get("upside_pct"), lin(valuation.get("upside_pct"), -40, 40)),
        },
        "quality": {
            "roic": (m.get("roic"), lin(m.get("roic"), 0.0, 0.25)),
            "roe": (m.get("roe"), lin(m.get("roe"), 0.0, 0.20)),
            "gross_margin": (m.get("gross_margin"), lin(m.get("gross_margin"), 0.10, 0.60)),
            "operating_margin": (m.get("operating_margin"), lin(m.get("operating_margin"), 0.0, 0.30)),
            "piotroski": (piotroski, lin(piotroski, 0, 9)),
            "interest_coverage": (m.get("interest_coverage"), lin(m.get("interest_coverage"), 1.5, 15)),
            "net_debt_to_ebitda": (m.get("net_debt_to_ebitda"), lin(m.get("net_debt_to_ebitda"), 4.0, 0.0)),
            "accruals_ratio": (m.get("accruals_ratio"), lin(m.get("accruals_ratio"), 0.10, -0.05)),
            "cash_conversion": (m.get("cash_conversion"), lin(m.get("cash_conversion"), 0.3, 1.2)),
        },
        "growth": {
            "revenue_cagr": (m.get("revenue_cagr"), lin(m.get("revenue_cagr"), -0.05, 0.20)),
            "revenue_growth_yoy": (m.get("revenue_growth_yoy"), lin(m.get("revenue_growth_yoy"), -0.05, 0.20)),
            "eps_growth_yoy": (m.get("eps_growth_yoy"), lin(m.get("eps_growth_yoy"), -0.10, 0.25)),
            "fcf_cagr": (m.get("fcf_cagr"), lin(m.get("fcf_cagr"), -0.10, 0.25)),
            "gross_margin_trend": (m.get("gross_margin_trend"), lin(m.get("gross_margin_trend"), -0.03, 0.03)),
        },
        "momentum": {
            "return_12_1m": (prices.get("return_12_1m"), lin(prices.get("return_12_1m"), -0.30, 0.40)),
            "return_6m": (prices.get("return_6m"), lin(prices.get("return_6m"), -0.20, 0.25)),
            "price_vs_ma200": (prices.get("price_vs_ma200"), lin(prices.get("price_vs_ma200"), -0.15, 0.15)),
        },
        "low_risk": {
            "volatility_1y": (prices.get("volatility_1y"), lin(prices.get("volatility_1y"), 0.60, 0.15)),
            "beta": (snapshot.get("market", {}).get("beta"), lin(snapshot.get("market", {}).get("beta"), 2.0, 0.6)),
            "max_drawdown_1y": (prices.get("max_drawdown_1y"), lin(prices.get("max_drawdown_1y"), -0.50, -0.10)),
        },
        "street": {
            "target_upside_pct": (valuation.get("street", {}).get("upside_to_mean_pct"),
                                  lin(valuation.get("street", {}).get("upside_to_mean_pct"), -10, 30)),
            "recommendation_mean": (info.get("recommendationMean"), lin(info.get("recommendationMean"), 4.0, 1.5)),
            "avg_earnings_surprise": (_avg_surprise(street), lin(_avg_surprise(street), -0.05, 0.10)),
        },
    }

    factors: Dict[str, Dict[str, Any]] = {}
    used = total = 0
    for name, items in subs.items():
        excluded = NOT_FOR_FINANCIALS if is_fin else FINANCIALS_ONLY
        applicable = {k: v for k, v in items.items() if k not in excluded}
        scored = {k: v[1] for k, v in applicable.items() if v[1] is not None}
        used += len(scored)
        total += len(applicable)
        factors[name] = {
            "score": round(sum(scored.values()) / len(scored), 1) if scored else None,
            "inputs": {k: {"value": (round(v[0], 4) if isinstance(v[0], float) else v[0]),
                           "score": (round(v[1], 1) if v[1] is not None else None)} for k, v in applicable.items()},
        }

    weighted = [(FACTOR_WEIGHTS[k], f["score"]) for k, f in factors.items() if f["score"] is not None]
    composite = round(sum(w * s for w, s in weighted) / sum(w for w, _ in weighted), 1) if weighted else None
    notes: List[str] = []
    currency = valuation.get("currency") or fundamentals.get("currency") or {}
    if currency.get("mismatch"):
        notes.append(f"Statements in {currency.get('financial')} vs price in {currency.get('trading')}: value "
                     f"metrics unavailable (not scored)")
    if factors["value"]["score"] is None:
        notes.append("No value metric available: composite excludes valuation")
    return {
        "composite": composite,
        "factors": factors,
        "weights": FACTOR_WEIGHTS,
        "coverage": round(used / total, 3) if total else 0.0,
        "notes": notes,
    }


def rank_universe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add cross-sectional percentile ranks (0–100) for composite and each factor; sort best first."""
    keys = ["composite"] + list(FACTOR_WEIGHTS)
    for key in keys:
        vals = [(i, r["scores"].get(key)) for i, r in enumerate(rows) if r.get("scores") and r["scores"].get(key) is not None]
        if len(vals) < 2:
            continue
        order = sorted(vals, key=lambda t: t[1])
        n = len(order)
        for rank, (i, _) in enumerate(order):
            rows[i].setdefault("percentiles", {})[key] = round(100 * rank / (n - 1), 1)
    return sorted(rows, key=lambda r: (r.get("scores") or {}).get("composite") or -1, reverse=True)
