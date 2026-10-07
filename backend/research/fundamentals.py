"""
Fundamental analysis from normalised annual statements (see data.py).

Everything here is pure arithmetic on reported numbers. Missing inputs yield
None rather than a guess, and each composite score reports how many of its
inputs were actually available.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

FINANCIAL_SECTORS = {"Financial Services", "Financials", "Banks", "Insurance"}


def _series(statements: Dict[str, Dict[str, float]], item: str) -> List[Optional[float]]:
    """Values of `item` across periods (oldest → newest)."""
    return [statements[p].get(item) for p in sorted(statements)]


def _last(xs: List[Optional[float]], k: int = 1) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return vals[-k] if len(vals) >= k else None


def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _avg(*xs: Optional[float]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _cagr(first: Optional[float], last: Optional[float], years: int) -> Optional[float]:
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def merge_annual(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """Combine income, balance and cash-flow statements per fiscal period."""
    annual = snapshot.get("statements", {}).get("annual", {})
    periods: Dict[str, Dict[str, float]] = {}
    for kind in ("income", "balance", "cashflow"):
        for period, row in (annual.get(kind) or {}).items():
            periods.setdefault(period, {}).update(row)
    for row in periods.values():
        if "fcf" not in row and row.get("operating_cf") is not None and row.get("capex") is not None:
            row["fcf"] = row["operating_cf"] + row["capex"]  # capex is reported negative
        if "ebit" not in row and row.get("operating_income") is not None:
            row["ebit"] = row["operating_income"]
    return periods


def piotroski_f_score(periods: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    """Piotroski (2000) nine binary tests comparing the last two fiscal years."""
    keys = sorted(periods)
    if len(keys) < 2:
        return {"score": None, "tests_available": 0, "tests": {}}
    prev, cur = periods[keys[-2]], periods[keys[-1]]

    def roa(p):
        return _div(p.get("net_income"), p.get("total_assets"))

    def lev(p):
        return _div(p.get("long_term_debt", p.get("total_debt")), p.get("total_assets"))

    def cr(p):
        return _div(p.get("current_assets"), p.get("current_liabilities"))

    def gm(p):
        return _div(p.get("gross_profit"), p.get("revenue"))

    def turnover(p):
        return _div(p.get("revenue"), p.get("total_assets"))

    def cmp(a, b, better="higher"):
        if a is None or b is None:
            return None
        return a > b if better == "higher" else a < b

    shares_cur = cur.get("shares_outstanding", cur.get("diluted_shares"))
    shares_prev = prev.get("shares_outstanding", prev.get("diluted_shares"))
    tests = {
        "positive_roa": None if roa(cur) is None else roa(cur) > 0,
        "positive_operating_cash_flow": None if cur.get("operating_cf") is None else cur["operating_cf"] > 0,
        "improving_roa": cmp(roa(cur), roa(prev)),
        "cash_flow_exceeds_net_income": cmp(cur.get("operating_cf"), cur.get("net_income")),
        "lower_leverage": cmp(lev(cur), lev(prev), "lower"),
        "higher_current_ratio": cmp(cr(cur), cr(prev)),
        "no_share_dilution": None if shares_cur is None or shares_prev is None else shares_cur <= shares_prev * 1.005,
        "higher_gross_margin": cmp(gm(cur), gm(prev)),
        "higher_asset_turnover": cmp(turnover(cur), turnover(prev)),
    }
    available = [v for v in tests.values() if v is not None]
    return {"score": sum(bool(v) for v in available) if len(available) >= 6 else None,
            "tests_available": len(available), "tests": tests}


def altman_z(latest: Dict[str, float], market_cap: Optional[float], sector: str) -> Dict[str, Any]:
    """Altman Z-score (1968, public companies). Not meaningful for banks/insurers."""
    if sector in FINANCIAL_SECTORS:
        return {"z": None, "zone": "not_applicable", "note": "Altman Z is not designed for financial companies"}
    ta = latest.get("total_assets")
    tl = latest.get("total_liabilities")
    wc = latest.get("working_capital")
    if wc is None and latest.get("current_assets") is not None and latest.get("current_liabilities") is not None:
        wc = latest["current_assets"] - latest["current_liabilities"]
    parts = [wc, latest.get("retained_earnings"), latest.get("ebit"), market_cap, latest.get("revenue")]
    if not ta or not tl or any(p is None for p in parts):
        return {"z": None, "zone": "insufficient_data"}
    z = 1.2 * wc / ta + 1.4 * latest["retained_earnings"] / ta + 3.3 * latest["ebit"] / ta \
        + 0.6 * market_cap / tl + 1.0 * latest["revenue"] / ta
    zone = "safe" if z > 2.99 else "grey" if z >= 1.81 else "distress"
    return {"z": round(z, 3), "zone": zone}


def analyze_fundamentals(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    periods = merge_annual(snapshot)
    keys = sorted(periods)
    market = snapshot.get("market", {})
    profile = snapshot.get("profile", {})
    sector = profile.get("sector", "")
    if not keys:
        return {"available": False, "periods": [], "reason": "No annual statements available"}

    latest = periods[keys[-1]]
    prev = periods[keys[-2]] if len(keys) >= 2 else {}

    def s(item):
        return _series(periods, item)

    revenue, ni, fcf = s("revenue"), s("net_income"), s("fcf")
    n_years = len(keys) - 1
    tax_rate = _div(latest.get("tax"), latest.get("pretax_income"))
    tax_rate = min(max(tax_rate, 0.0), 0.35) if tax_rate is not None else 0.21
    ebit = latest.get("ebit")
    invested = None
    if latest.get("equity") is not None:
        invested = latest["equity"] + (latest.get("total_debt") or 0) - (latest.get("cash") or 0)
    nopat = ebit * (1 - tax_rate) if ebit is not None else None
    net_debt = (latest.get("total_debt") or 0) - (latest.get("cash") or 0) if latest.get("total_debt") is not None else None
    market_cap = market.get("market_cap")
    shareholder_return = None
    if market_cap:
        paid = -(latest.get("dividends_paid") or 0) - (latest.get("buybacks") or 0)
        shareholder_return = paid / market_cap

    gross_margins = [_div(p.get("gross_profit"), p.get("revenue")) for p in (periods[k] for k in keys)]
    op_margins = [_div(p.get("ebit"), p.get("revenue")) for p in (periods[k] for k in keys)]
    net_margins = [_div(p.get("net_income"), p.get("revenue")) for p in (periods[k] for k in keys)]
    fcf_margins = [_div(p.get("fcf"), p.get("revenue")) for p in (periods[k] for k in keys)]

    def trend(xs):
        vals = [x for x in xs if x is not None]
        return vals[-1] - sum(vals[:-1]) / len(vals[:-1]) if len(vals) >= 2 else None

    shares_now = latest.get("diluted_shares") or latest.get("shares_outstanding")
    shares_prev = prev.get("diluted_shares") or prev.get("shares_outstanding")

    metrics = {
        "revenue": _last(revenue),
        "revenue_growth_yoy": _div(_last(revenue), _last(revenue, 2)) - 1 if _div(_last(revenue), _last(revenue, 2)) else None,
        "revenue_cagr": _cagr(revenue[0], revenue[-1], n_years),
        "net_income": _last(ni),
        "eps_growth_yoy": (_div(latest.get("diluted_eps"), prev.get("diluted_eps")) - 1)
        if prev.get("diluted_eps") and prev["diluted_eps"] > 0 and latest.get("diluted_eps") is not None else None,
        "fcf": _last(fcf),
        "fcf_cagr": _cagr(fcf[0], fcf[-1], n_years),
        "gross_margin": gross_margins[-1],
        "operating_margin": op_margins[-1],
        "net_margin": net_margins[-1],
        "fcf_margin": fcf_margins[-1],
        "gross_margin_trend": trend(gross_margins),
        "operating_margin_trend": trend(op_margins),
        "roe": _div(latest.get("net_income"), _avg(latest.get("equity"), prev.get("equity"))),
        "roa": _div(latest.get("net_income"), latest.get("total_assets")),
        "roic": _div(nopat, invested) if invested and invested > 0 else None,
        "debt_to_equity": _div(latest.get("total_debt"), latest.get("equity")) if (latest.get("equity") or 0) > 0 else None,
        "net_debt": net_debt,
        "net_debt_to_ebitda": _div(net_debt, latest.get("ebitda")) if (latest.get("ebitda") or 0) > 0 else None,
        "interest_coverage": _div(ebit, abs(latest["interest_expense"])) if latest.get("interest_expense") else None,
        "current_ratio": _div(latest.get("current_assets"), latest.get("current_liabilities")),
        "cash_conversion": _div(latest.get("fcf"), latest.get("net_income")) if (latest.get("net_income") or 0) > 0 else None,
        "accruals_ratio": _div((latest.get("net_income") or 0) - (latest.get("operating_cf") or 0), latest.get("total_assets"))
        if latest.get("operating_cf") is not None and latest.get("net_income") is not None else None,
        "share_count_change": _div(shares_now, shares_prev) - 1 if shares_now and shares_prev else None,
        "sbc_to_revenue": _div(latest.get("sbc"), latest.get("revenue")),
        "shareholder_yield": shareholder_return,
        "tax_rate": tax_rate,
    }
    history = [
        {
            "period": k,
            "revenue": periods[k].get("revenue"),
            "net_income": periods[k].get("net_income"),
            "fcf": periods[k].get("fcf"),
            "gross_margin": gross_margins[i],
            "operating_margin": op_margins[i],
            "net_margin": net_margins[i],
            "diluted_eps": periods[k].get("diluted_eps"),
        }
        for i, k in enumerate(keys)
    ]

    flags: List[str] = []
    if metrics["fcf"] is not None and metrics["fcf"] < 0:
        flags.append("Negative free cash flow")
    if metrics["net_debt_to_ebitda"] is not None and metrics["net_debt_to_ebitda"] > 3.5:
        flags.append(f"High leverage: net debt/EBITDA {metrics['net_debt_to_ebitda']:.1f}x")
    if metrics["interest_coverage"] is not None and metrics["interest_coverage"] < 3:
        flags.append(f"Thin interest coverage ({metrics['interest_coverage']:.1f}x)")
    if metrics["share_count_change"] is not None and metrics["share_count_change"] > 0.03:
        flags.append(f"Share dilution {metrics['share_count_change']:.1%} YoY")
    if metrics["accruals_ratio"] is not None and metrics["accruals_ratio"] > 0.10:
        flags.append("Earnings well above cash flow (high accruals)")
    if metrics["revenue_growth_yoy"] is not None and metrics["revenue_growth_yoy"] < -0.05:
        flags.append(f"Revenue declining {metrics['revenue_growth_yoy']:.1%} YoY")

    piotroski = piotroski_f_score(periods)
    altman = altman_z(latest, market_cap, sector)
    if altman.get("zone") == "distress":
        flags.append(f"Altman Z {altman['z']} in distress zone")
    if piotroski.get("score") is not None and piotroski["score"] <= 2:
        flags.append(f"Weak Piotroski F-score ({piotroski['score']}/9)")

    return {
        "available": True,
        "periods": keys,
        "latest_period": keys[-1],
        "is_financial": sector in FINANCIAL_SECTORS,
        "metrics": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()},
        "history": history,
        "piotroski": piotroski,
        "altman": altman,
        "flags": flags,
    }
