"""
Fundamental analysis from normalised annual statements (see data.py).

Everything here is pure arithmetic on reported numbers. Missing inputs yield
None rather than a guess, and each composite score reports how many of its
inputs were actually available.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TAX_RATE = 0.21

# ─── Business-model classification ────────────────────────────────────────────
#
# Yahoo's *sector* "Financial Services" lumps deposit-funded banks together with
# asset-light payment networks (Visa, Mastercard, PayPal are "Credit Services"),
# exchanges and brokers. Only balance-sheet financials — whose assets are
# financial instruments funded by deposits, policyholder float or wholesale
# debt — need the bank toolkit (justified P/B; no FCF/EV/gross margin/Altman).
# Classification is therefore by INDUSTRY (normalised, so "Banks—Regional" and
# "Banks - Regional" match), with a balance-sheet test for ambiguous industries.

FINANCIAL_SECTORS = {"Financial Services", "Financials", "Banks", "Insurance"}

# Always balance-sheet financials (prefix match on the normalised industry).
BALANCE_SHEET_INDUSTRIES = (
    "banks", "insurance", "capital markets", "mortgage finance", "thrifts", "savings",
    "financial conglomerates", "diversified financial", "reit mortgage", "mortgage reit", "shell companies",
)
# Financial industries that contain both asset-light businesses (payment
# networks, exchanges, data vendors, brokers, traditional fund managers) and
# lenders / consolidated-fund vehicles: decided by the balance-sheet test.
ASSET_LIGHT_CANDIDATE_INDUSTRIES = (
    "credit services", "consumer finance", "transaction payment processing", "asset management",
    "financial data", "financial exchanges", "insurance brokers",
)
# Asset-light test: a lender carries its loan book on the balance sheet, so its
# total assets are many times revenue (banks ~10x+, card lenders ~5x); payment
# networks and fee businesses sit around 1-3x. Must also generate positive FCF.
ASSET_LIGHT_MAX_ASSETS_TO_REVENUE = 3.0


def _norm_text(s: Optional[str]) -> str:
    return re.sub(r"[^a-z]+", " ", str(s or "").lower()).strip()


def classify_business(sector: str, industry: str, latest: Dict[str, float]) -> Dict[str, Any]:
    """
    → {"type": "balance_sheet_financial" | "asset_light_financial" | "operating",
       "is_financial": bool (True only for the balance-sheet model), "reason": str}
    """
    ind = _norm_text(industry)

    def asset_light_test(label: str) -> Dict[str, Any]:
        ta, rev = latest.get("total_assets"), latest.get("revenue")
        fcf = latest.get("fcf")
        intensity = _div(ta, rev) if rev and rev > 0 else None
        if intensity is not None and intensity <= ASSET_LIGHT_MAX_ASSETS_TO_REVENUE and fcf is not None and fcf > 0:
            return {"type": "asset_light_financial", "is_financial": False,
                    "reason": f"{label}: asset-light (assets {intensity:.1f}x revenue, positive FCF) — valued as an "
                              f"operating business"}
        detail = f"assets {intensity:.1f}x revenue" if intensity is not None else "assets/revenue unavailable"
        return {"type": "balance_sheet_financial", "is_financial": True,
                "reason": f"{label}: balance-sheet intensive ({detail}"
                          f"{'' if fcf is not None and fcf > 0 else ', no positive FCF'}) — bank-style model"}

    if any(ind.startswith(p) for p in ASSET_LIGHT_CANDIDATE_INDUSTRIES):
        return asset_light_test(f"Industry '{industry}'")
    if any(ind.startswith(p) for p in BALANCE_SHEET_INDUSTRIES):
        return {"type": "balance_sheet_financial", "is_financial": True,
                "reason": f"Industry '{industry}' is a balance-sheet financial (bank/insurer/broker-dealer)"}
    if sector in FINANCIAL_SECTORS:
        return asset_light_test(f"Sector '{sector}' with industry '{industry or 'unknown'}'")
    return {"type": "operating", "is_financial": False, "reason": "Operating company"}


def currency_info(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trading (price) vs reporting (statement) currency. A mismatch (ADRs such as
    TSM: USD per ADR vs TWD statements; London lines quoted in GBp vs GBP
    statements) makes every ratio mixing price with statement figures
    meaningless without FX conversion, which is not done offline. Compared
    case-sensitively on purpose: "GBp" (pence) is not "GBP".
    """
    profile = snapshot.get("profile") or {}
    trading = profile.get("trading_currency")
    financial = profile.get("financial_currency")
    return {"trading": trading, "financial": financial,
            "mismatch": bool(trading and financial and trading != financial)}


# ─── Altman Z model selection ─────────────────────────────────────────────────
#
# The 1968 Z-score was fitted on US manufacturers; its sales/assets and
# market-cap terms misfire on services, tech and asset-heavy regulated
# businesses. Mapping (sector from Yahoo/GICS names, refined by industry):
#   * not applicable: balance-sheet financials, Utilities, Real Estate
#     (regulated / REIT capital structures run high asset-backed leverage);
#   * Z (1968, manufacturers): Industrials, Basic Materials, Energy, Consumer
#     Cyclical/Defensive, Healthcare — unless the industry is a service/retail/
#     distribution/transport business (keyword list below);
#   * Z' (1983, book equity): a manufacturer whose market value is unavailable
#     or quoted in a different currency from its statements;
#   * Z'' (1995, non-manufacturers): everything else — Technology,
#     Communication Services, asset-light financials, service industries above,
#     and unknown sectors.

ALTMAN_NOT_APPLICABLE_SECTORS = {"Utilities", "Real Estate"}
MANUFACTURING_SECTORS = {
    "Industrials", "Basic Materials", "Materials", "Energy", "Consumer Cyclical", "Consumer Discretionary",
    "Consumer Defensive", "Consumer Staples", "Healthcare", "Health Care",
}
NON_MANUFACTURING_INDUSTRY_KEYWORDS = (
    "retail", "stores", "dealerships", "restaurants", "lodging", "resorts", "casinos", "gambling",
    "travel services", "personal services", "education", "staffing", "consulting", "rental",
    "security protection", "specialty business services", "waste management", "engineering construction",
    "infrastructure operations", "airlines", "airports", "trucking", "railroads", "marine shipping",
    "freight", "logistics", "distribution", "midstream", "healthcare plans", "medical care facilities",
    "health information services", "diagnostics research",
)

ALTMAN_MODELS = {
    "z": {"name": "Altman Z (1968, public manufacturers)", "distress": 1.81, "safe": 2.99},
    "z_prime": {"name": "Altman Z' (1983, book-equity manufacturers)", "distress": 1.23, "safe": 2.90},
    "z_double_prime": {"name": "Altman Z'' (1995, non-manufacturers)", "distress": 1.1, "safe": 2.6},
}


def altman_model_for(sector: str, industry: str, is_financial: bool) -> Tuple[Optional[str], str]:
    ind = _norm_text(industry)
    if is_financial:
        return None, "Altman Z is not designed for banks, insurers and other balance-sheet financials"
    if sector in ALTMAN_NOT_APPLICABLE_SECTORS or ind.startswith("reit") or ind.startswith("utilities"):
        return None, ("Altman Z is not meaningful for utilities and REITs: regulated / REIT capital structures "
                      "carry high, asset-backed leverage by design")
    if sector in MANUFACTURING_SECTORS and not any(k in ind for k in NON_MANUFACTURING_INDUSTRY_KEYWORDS):
        return "z", f"Manufacturing sector '{sector}'"
    return "z_double_prime", f"Non-manufacturer ('{sector or 'unknown sector'}' / '{industry or 'unknown industry'}')"


def effective_tax_rate(row: Dict[str, float]) -> float:
    """Tax provision / pretax income, clamped to [0, 35%]; 21% when unavailable."""
    r = _div(row.get("tax"), row.get("pretax_income"))
    return min(max(r, 0.0), 0.35) if r is not None else DEFAULT_TAX_RATE


def fcff(row: Dict[str, float]) -> Optional[float]:
    """
    Free cash flow to the firm: levered FCF (CFO − capex, which is after interest)
    plus after-tax interest expense. This is the cash flow a WACC-discounted,
    enterprise-value DCF must use before net debt is subtracted.
    """
    fcf = row.get("fcf")
    if fcf is None:
        return None
    interest = row.get("interest_expense")
    return fcf + abs(interest) * (1 - effective_tax_rate(row)) if interest else fcf


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


def altman_z(latest: Dict[str, float], market_cap: Optional[float], sector: str, industry: str = "",
             is_financial: Optional[bool] = None, market_cap_comparable: bool = True) -> Dict[str, Any]:
    """
    Altman bankruptcy score with the model variant that fits the business
    (see ALTMAN_* above). `market_cap_comparable` is False when the market cap is
    quoted in a different currency from the statements (it is then not used).
    """
    if is_financial is None:
        is_financial = classify_business(sector, industry, latest)["is_financial"]
    model, why = altman_model_for(sector, industry, is_financial)
    if model is None:
        return {"z": None, "zone": "not_applicable", "model": None, "note": why}
    if model == "z" and (market_cap is None or not market_cap_comparable):
        model = "z_prime"
        why += "; market value unusable (missing or different currency), so the book-equity Z' variant is used"
    spec = ALTMAN_MODELS[model]

    ta = latest.get("total_assets")
    tl = latest.get("total_liabilities")
    wc = latest.get("working_capital")
    if wc is None and latest.get("current_assets") is not None and latest.get("current_liabilities") is not None:
        wc = latest["current_assets"] - latest["current_liabilities"]
    re_, ebit, sales, book = latest.get("retained_earnings"), latest.get("ebit"), latest.get("revenue"), latest.get("equity")
    needed = {"z": [wc, re_, ebit, market_cap, sales], "z_prime": [wc, re_, ebit, book, sales],
              "z_double_prime": [wc, re_, ebit, book]}[model]
    base = {"model": model, "model_name": spec["name"], "note": why,
            "thresholds": {"distress_below": spec["distress"], "safe_above": spec["safe"]}}
    if not ta or not tl or any(p is None for p in needed):
        return {"z": None, "zone": "insufficient_data", **base}
    if model == "z":
        z = 1.2 * wc / ta + 1.4 * re_ / ta + 3.3 * ebit / ta + 0.6 * market_cap / tl + 1.0 * sales / ta
    elif model == "z_prime":
        z = 0.717 * wc / ta + 0.847 * re_ / ta + 3.107 * ebit / ta + 0.420 * book / tl + 0.998 * sales / ta
    else:
        z = 6.56 * wc / ta + 3.26 * re_ / ta + 6.72 * ebit / ta + 1.05 * book / tl
    zone = "safe" if z > spec["safe"] else "grey" if z >= spec["distress"] else "distress"
    return {"z": round(z, 3), "zone": zone, **base}


def analyze_fundamentals(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    periods = merge_annual(snapshot)
    keys = sorted(periods)
    market = snapshot.get("market", {})
    profile = snapshot.get("profile", {})
    sector = profile.get("sector", "") or ""
    industry = profile.get("industry", "") or ""
    currency = currency_info(snapshot)
    if not keys:
        return {"available": False, "periods": [], "reason": "No annual statements available",
                "currency": currency}

    latest = periods[keys[-1]]
    prev = periods[keys[-2]] if len(keys) >= 2 else {}
    business = classify_business(sector, industry, latest)

    def s(item):
        return _series(periods, item)

    revenue, ni, fcf = s("revenue"), s("net_income"), s("fcf")
    n_years = len(keys) - 1
    tax_rate = effective_tax_rate(latest)
    ebit = latest.get("ebit")
    invested = None
    if latest.get("equity") is not None:
        invested = latest["equity"] + (latest.get("total_debt") or 0) - (latest.get("cash") or 0)
    nopat = ebit * (1 - tax_rate) if ebit is not None else None
    net_debt = (latest.get("total_debt") or 0) - (latest.get("cash") or 0) if latest.get("total_debt") is not None else None
    market_cap = market.get("market_cap")
    shareholder_return = None
    if market_cap and not currency["mismatch"]:  # never divide statement cash flows by a foreign-currency cap
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
        "fcff": fcff(latest),
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
            "fcff": fcff(periods[k]),
            "interest_expense": periods[k].get("interest_expense"),
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
    altman = altman_z(latest, market_cap, sector, industry, is_financial=business["is_financial"],
                      market_cap_comparable=not currency["mismatch"])
    if altman.get("zone") == "distress":
        flags.append(f"{ALTMAN_MODELS[altman['model']]['name'].split(' (')[0]} {altman['z']} in distress zone")
    if piotroski.get("score") is not None and piotroski["score"] <= 2:
        flags.append(f"Weak Piotroski F-score ({piotroski['score']}/9)")

    return {
        "available": True,
        "periods": keys,
        "latest_period": keys[-1],
        "is_financial": business["is_financial"],
        "business_model": business,
        "currency": currency,
        "metrics": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()},
        "history": history,
        "piotroski": piotroski,
        "altman": altman,
        "flags": flags,
    }
