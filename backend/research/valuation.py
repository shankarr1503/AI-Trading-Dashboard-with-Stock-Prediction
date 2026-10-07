"""
Valuation: trading multiples, cost of capital, scenario DCF, reverse DCF and
(for banks/insurers) a justified price-to-book model.

These are models, not oracles: every output carries its assumptions so a
reader (or the analyst agent) can judge them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

RISK_FREE = 0.0425      # long-run US 10y Treasury assumption
EQUITY_RISK_PREMIUM = 0.05
SCENARIO_PROBS = {"bear": 0.25, "base": 0.50, "bull": 0.25}


def _num(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and x == x else None


def cost_of_capital(beta: Optional[float], market_cap: Optional[float], total_debt: Optional[float],
                    interest_expense: Optional[float], tax_rate: float) -> Dict[str, float]:
    b = min(max(beta if beta is not None else 1.0, 0.5), 2.5)
    cost_equity = RISK_FREE + b * EQUITY_RISK_PREMIUM
    if total_debt and interest_expense:
        pre_tax_debt = min(max(abs(interest_expense) / total_debt, 0.03), 0.12)
    else:
        pre_tax_debt = RISK_FREE + 0.015
    cost_debt = pre_tax_debt * (1 - tax_rate)
    e = market_cap or 0.0
    d = total_debt or 0.0
    wacc = cost_equity if e + d == 0 else (e * cost_equity + d * cost_debt) / (e + d)
    return {
        "beta_used": round(b, 3),
        "cost_of_equity": round(cost_equity, 4),
        "cost_of_debt_after_tax": round(cost_debt, 4),
        "wacc": round(min(max(wacc, 0.06), 0.14), 4),
    }


def dcf_enterprise_value(fcf0: float, growth: float, terminal_growth: float, wacc: float, years: int = 10) -> float:
    """
    Two-stage DCF: growth fades linearly from `growth` (year 1) to
    `terminal_growth` (year `years`), then a Gordon-growth terminal value.
    """
    if wacc <= terminal_growth + 0.01:
        raise ValueError("WACC must exceed terminal growth by at least 1pp")
    pv, fcf = 0.0, fcf0
    for t in range(1, years + 1):
        g = growth + (terminal_growth - growth) * (t - 1) / max(1, years - 1)
        fcf *= 1 + g
        pv += fcf / (1 + wacc) ** t
    terminal = fcf * (1 + terminal_growth) / (wacc - terminal_growth)
    return pv + terminal / (1 + wacc) ** years


def implied_growth(target_ev: float, fcf0: float, terminal_growth: float, wacc: float) -> Optional[float]:
    """Reverse DCF: initial growth rate the current enterprise value implies."""
    if target_ev <= 0 or fcf0 <= 0:
        return None
    lo, hi = -0.30, 0.60
    if dcf_enterprise_value(fcf0, lo, terminal_growth, wacc) > target_ev:
        return lo
    if dcf_enterprise_value(fcf0, hi, terminal_growth, wacc) < target_ev:
        return hi
    for _ in range(80):
        mid = (lo + hi) / 2
        if dcf_enterprise_value(fcf0, mid, terminal_growth, wacc) < target_ev:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _analyst_growth(street: Dict[str, Any]) -> Optional[float]:
    """Best-effort +1y growth estimate from yfinance's estimate tables."""
    for table in ("revenue_estimate", "growth_estimates"):
        for rec in street.get(table) or []:
            period = str(rec.get("period") or rec.get("index") or "")
            if period in ("+1y", "0y"):
                for key in ("growth", "stock", "stockTrend"):
                    v = rec.get(key)
                    if isinstance(v, (int, float)) and -0.5 < v < 1.0:
                        return float(v)
    return None


def value_company(snapshot: Dict[str, Any], fundamentals: Dict[str, Any]) -> Dict[str, Any]:
    market = snapshot.get("market", {})
    info = snapshot.get("info_metrics", {})
    street = snapshot.get("street", {})
    price = _num(market.get("price"))
    market_cap = _num(market.get("market_cap"))
    m = fundamentals.get("metrics", {}) if fundamentals.get("available") else {}
    latest = {}
    if fundamentals.get("available"):
        from backend.research.fundamentals import merge_annual

        periods = merge_annual(snapshot)
        latest = periods[sorted(periods)[-1]]

    total_debt = _num(latest.get("total_debt")) or _num(info.get("totalDebt"))
    cash = _num(latest.get("cash")) or _num(info.get("totalCash"))
    ev = market_cap + (total_debt or 0) - (cash or 0) if market_cap else None
    shares = _num(latest.get("diluted_shares")) or _num(market.get("shares_outstanding"))
    if (not shares) and market_cap and price:
        shares = market_cap / price

    eps = _num(latest.get("diluted_eps"))
    ebitda = _num(latest.get("ebitda")) or _num(info.get("ebitda"))
    revenue = _num(latest.get("revenue")) or _num(info.get("totalRevenue"))
    fcf = _num(latest.get("fcf")) or _num(info.get("freeCashflow"))
    equity = _num(latest.get("equity"))

    def ratio(a, b):
        return round(a / b, 3) if a is not None and b not in (None, 0) and b > 0 else None

    multiples = {
        "pe": ratio(price, eps) or info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "peg": info.get("trailingPegRatio") or info.get("pegRatio"),
        "ev_ebitda": ratio(ev, ebitda),
        "ev_sales": ratio(ev, revenue),
        "price_to_fcf": ratio(market_cap, fcf),
        "fcf_yield": round(fcf / market_cap, 4) if fcf is not None and market_cap else None,
        "earnings_yield": round(eps / price, 4) if eps is not None and price else None,
        "price_to_book": ratio(market_cap, equity) or info.get("priceToBook"),
        "dividend_yield": market.get("dividend_yield"),
    }

    tax_rate = m.get("tax_rate") if m.get("tax_rate") is not None else 0.21
    coc = cost_of_capital(_num(market.get("beta")), market_cap, total_debt, _num(latest.get("interest_expense")), tax_rate)
    wacc = coc["wacc"]

    assumptions: List[str] = [
        f"Risk-free {RISK_FREE:.2%}, equity risk premium {EQUITY_RISK_PREMIUM:.1%}, beta {coc['beta_used']}",
        f"WACC {wacc:.2%} (clamped to 6–14%)",
    ]
    warnings: List[str] = []
    scenarios: Dict[str, Dict[str, Any]] = {}
    method = "none"
    implied = None

    analyst_g = _analyst_growth(street)
    hist_g = m.get("revenue_cagr")
    growth_inputs = [g for g in (analyst_g, hist_g) if g is not None]
    base_g = sum(growth_inputs) / len(growth_inputs) if growth_inputs else 0.04
    base_g = min(max(base_g, -0.05), 0.25)
    sources = []
    if analyst_g is not None:
        sources.append(f"analyst +1y {analyst_g:.1%}")
    if hist_g is not None:
        sources.append(f"historical revenue CAGR {hist_g:.1%}")
    assumptions.append(f"Base growth {base_g:.1%} ({', '.join(sources) or 'default'}), clamped to −5%…25%")

    if fundamentals.get("is_financial"):
        roe = m.get("roe")
        if roe is not None and equity and shares and equity > 0:
            bvps = equity / shares
            coe = coc["cost_of_equity"]
            g = 0.03
            method = "justified_pb"
            for name, roe_adj in (("bear", -0.03), ("base", 0.0), ("bull", 0.03)):
                r = roe + roe_adj
                pb = min(max((r - g) / (coe - g), 0.3), 4.0) if coe > g else None
                scenarios[name] = {"value": round(bvps * pb, 2) if pb else None, "roe": round(r, 4),
                                   "probability": SCENARIO_PROBS[name]}
            assumptions.append(f"Justified P/B = (ROE − g)/(COE − g), g = {g:.0%}, COE {coe:.2%}")
        else:
            warnings.append("Financial company without usable ROE/book value: no intrinsic value")
    else:
        hist_fcf = [h.get("fcf") for h in fundamentals.get("history", []) if h.get("fcf") is not None]
        recent = hist_fcf[-3:]
        fcf_base = None
        if recent and recent[-1] > 0 and sum(recent) > 0:
            fcf_base = 0.5 * recent[-1] + 0.5 * (sum(recent) / len(recent))
        if fcf_base and shares and ev is not None:
            net_debt = (total_debt or 0) - (cash or 0)
            method = "dcf"
            for name, dg, tg, dw in (("bear", -0.06, 0.02, 0.01), ("base", 0.0, 0.025, 0.0), ("bull", 0.05, 0.03, -0.005)):
                g = min(max(base_g + dg, -0.10), 0.35)
                w = wacc + dw
                ev_dcf = dcf_enterprise_value(fcf_base, g, tg, w)
                per_share = max(0.0, (ev_dcf - net_debt) / shares)
                scenarios[name] = {"value": round(per_share, 2), "growth": round(g, 4), "terminal_growth": tg,
                                   "wacc": round(w, 4), "probability": SCENARIO_PROBS[name]}
            implied = implied_growth(ev, fcf_base, 0.025, wacc)
            assumptions.append(f"Normalised FCF {fcf_base:,.0f} (blend of latest and 3-year average)")
        else:
            warnings.append("Free cash flow is negative or unavailable: DCF not meaningful")

    fair_value = None
    if scenarios and all(s.get("value") is not None for s in scenarios.values()):
        fair_value = round(sum(s["value"] * s["probability"] for s in scenarios.values()), 2)

    targets = street.get("price_targets") or {}
    t_mean = _num(targets.get("mean"))
    return {
        "price": price,
        "market_cap": market_cap,
        "enterprise_value": ev,
        "shares": shares,
        "multiples": multiples,
        "cost_of_capital": coc,
        "method": method,
        "scenarios": scenarios,
        "fair_value": fair_value,
        "upside_pct": round((fair_value / price - 1) * 100, 2) if fair_value and price else None,
        "market_implied_growth": round(implied, 4) if implied is not None else None,
        "street": {
            "target_mean": t_mean,
            "target_low": _num(targets.get("low")),
            "target_high": _num(targets.get("high")),
            "upside_to_mean_pct": round((t_mean / price - 1) * 100, 2) if t_mean and price else None,
        },
        "assumptions": assumptions,
        "warnings": warnings,
    }
