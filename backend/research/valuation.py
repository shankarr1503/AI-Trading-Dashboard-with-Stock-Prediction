"""
Valuation: trading multiples, cost of capital, scenario DCF, reverse DCF and
(for banks/insurers) a justified price-to-book model.

The DCF discounts free cash flow to the FIRM (levered FCF + after-tax interest)
at WACC to an enterprise value, then subtracts net debt — discounting levered
FCF at WACC and subtracting debt again would count the debt twice.

These are models, not oracles: every output carries its assumptions so a
reader (or the analyst agent) can judge them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

RISK_FREE = 0.0425      # long-run US 10y Treasury assumption
EQUITY_RISK_PREMIUM = 0.05
SCENARIO_PROBS = {"bear": 0.25, "base": 0.50, "bull": 0.25}
# Justified P/B is only trusted when the base case lands in this band: outside
# it, book value is not what drives the price (buyback-shrunk equity, fee
# businesses, write-downs) and clamping would collapse bear/base/bull together.
PB_VALID_RANGE = (0.3, 4.0)
PB_BEAR_FLOOR = 0.1
PRICE_MULTIPLES = ("pe", "forward_pe", "peg", "ev_ebitda", "ev_sales", "price_to_fcf", "fcf_yield",
                   "earnings_yield", "price_to_book")


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
    from backend.research.fundamentals import currency_info, merge_annual

    latest = {}
    if fundamentals.get("available"):
        periods = merge_annual(snapshot)
        latest = periods[sorted(periods)[-1]]
    currency = fundamentals.get("currency") or currency_info(snapshot)
    mismatch = bool(currency.get("mismatch"))

    total_debt = _num(latest.get("total_debt")) or _num(info.get("totalDebt"))
    cash = _num(latest.get("cash")) or _num(info.get("totalCash"))
    # EV adds a trading-currency market cap to statement-currency debt/cash.
    ev = market_cap + (total_debt or 0) - (cash or 0) if market_cap and not mismatch else None
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
    warnings: List[str] = []
    if mismatch:
        # Price is per trading-currency share (often per ADR); statements are in
        # another currency (and often per ordinary share). Without FX conversion
        # every price-vs-fundamentals ratio is meaningless, so report none.
        multiples.update({k: None for k in PRICE_MULTIPLES})
        warnings.append(f"Financial statements are reported in {currency.get('financial')} but the stock trades in "
                        f"{currency.get('trading')}: price multiples and intrinsic value are not computed "
                        f"(no FX conversion)")

    tax_rate = m.get("tax_rate") if m.get("tax_rate") is not None else 0.21
    # Capital-structure weights would mix currencies too: fall back to equity-only.
    coc = cost_of_capital(_num(market.get("beta")), market_cap, None if mismatch else total_debt,
                          _num(latest.get("interest_expense")), tax_rate)
    wacc = coc["wacc"]

    assumptions: List[str] = [
        f"Risk-free {RISK_FREE:.2%}, equity risk premium {EQUITY_RISK_PREMIUM:.1%}, beta {coc['beta_used']}",
        f"WACC {wacc:.2%} (clamped to 6–14%)" + (" — equity-only weights (currency mismatch)" if mismatch else ""),
    ]
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

    fcff_base = None
    if mismatch:
        pass  # warned above: no intrinsic value across currencies
    elif fundamentals.get("is_financial"):
        roe = m.get("roe")
        coe = coc["cost_of_equity"]
        g = 0.03
        if roe is not None and equity and shares and equity > 0 and coe > g:
            bvps = equity / shares
            base_pb = (roe - g) / (coe - g)
            lo, hi = PB_VALID_RANGE
            if lo <= base_pb <= hi:
                method = "justified_pb"
                for name, roe_adj in (("bear", -0.03), ("base", 0.0), ("bull", 0.03)):
                    r = roe + roe_adj
                    pb = max((r - g) / (coe - g), PB_BEAR_FLOOR)
                    scenarios[name] = {"value": round(bvps * pb, 2), "roe": round(r, 4), "pb": round(pb, 3),
                                       "probability": SCENARIO_PROBS[name]}
                assumptions.append(f"Justified P/B = (ROE − g)/(COE − g), g = {g:.0%}, COE {coe:.2%}: base "
                                   f"{base_pb:.2f}x book; scenarios ROE ±3pp (floored at {PB_BEAR_FLOOR}x book)")
            else:
                warnings.append(
                    f"Justified P/B not applicable: ROE {roe:.1%} vs cost of equity {coe:.2%} implies "
                    f"{base_pb:.2f}x book, outside the model's {lo}–{hi}x validity range (book value does not "
                    f"anchor this business): no intrinsic value")
        else:
            warnings.append("Financial company without usable ROE/book value: no intrinsic value")
    else:
        hist = [h.get("fcff", h.get("fcf")) for h in fundamentals.get("history", [])]
        recent = [x for x in hist if x is not None][-3:]
        if recent and recent[-1] > 0 and sum(recent) > 0:
            fcff_base = 0.5 * recent[-1] + 0.5 * (sum(recent) / len(recent))
        if fcff_base and shares and ev is not None:
            net_debt = (total_debt or 0) - (cash or 0)
            method = "dcf"
            for name, dg, tg, dw in (("bear", -0.06, 0.02, 0.01), ("base", 0.0, 0.025, 0.0), ("bull", 0.05, 0.03, -0.005)):
                g = min(max(base_g + dg, -0.10), 0.35)
                w = wacc + dw
                ev_dcf = dcf_enterprise_value(fcff_base, g, tg, w)
                per_share = max(0.0, (ev_dcf - net_debt) / shares)
                scenarios[name] = {"value": round(per_share, 2), "growth": round(g, 4), "terminal_growth": tg,
                                   "wacc": round(w, 4), "probability": SCENARIO_PROBS[name]}
            implied = implied_growth(ev, fcff_base, 0.025, wacc)
            assumptions.append(f"Normalised FCFF {fcff_base:,.0f} (levered FCF + after-tax interest; blend of latest "
                               f"and 3-year average), discounted at WACC, less net debt {net_debt:,.0f}")
            if latest.get("interest_expense") is None and (total_debt or 0) > 0:
                assumptions.append("Interest expense not reported: FCFF taken as levered FCF (conservative)")
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
        "currency": currency,
        "fcff_base": round(fcff_base, 2) if fcff_base else None,
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
