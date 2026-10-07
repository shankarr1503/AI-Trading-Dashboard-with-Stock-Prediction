"""
Company data snapshot from Yahoo Finance, normalised into plain JSON-safe dicts.

yfinance payload shapes change between versions and many fields are missing for
smaller or non-US companies, so every section is fetched defensively and
failures are recorded in `data_gaps` instead of raising.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# Canonical statement items → yfinance row labels (compared after normalising
# case/spaces, so both "TotalRevenue" and "Total Revenue" match).
STATEMENT_ITEMS: Dict[str, List[str]] = {
    "revenue": ["TotalRevenue", "OperatingRevenue"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncome", "TotalOperatingIncomeAsReported"],
    "ebit": ["EBIT"],
    "ebitda": ["EBITDA", "NormalizedEBITDA"],
    "net_income": ["NetIncomeCommonStockholders", "NetIncome"],
    "pretax_income": ["PretaxIncome"],
    "tax": ["TaxProvision"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonOperating"],
    "diluted_eps": ["DilutedEPS"],
    "diluted_shares": ["DilutedAverageShares"],
    "total_assets": ["TotalAssets"],
    "current_assets": ["CurrentAssets"],
    "current_liabilities": ["CurrentLiabilities"],
    "total_liabilities": ["TotalLiabilitiesNetMinorityInterest"],
    "total_debt": ["TotalDebt"],
    "long_term_debt": ["LongTermDebt"],
    "cash": ["CashAndCashEquivalents", "CashCashEquivalentsAndShortTermInvestments"],
    "equity": ["StockholdersEquity", "CommonStockEquity"],
    "retained_earnings": ["RetainedEarnings"],
    "working_capital": ["WorkingCapital"],
    "shares_outstanding": ["OrdinarySharesNumber", "ShareIssued"],
    "operating_cf": ["OperatingCashFlow"],
    "capex": ["CapitalExpenditure"],
    "fcf": ["FreeCashFlow"],
    "dividends_paid": ["CashDividendsPaid", "CommonStockDividendPaid"],
    "buybacks": ["RepurchaseOfCapitalStock", "CommonStockPayments"],
    "sbc": ["StockBasedCompensation"],
}

INFO_FIELDS = [
    "trailingPE", "forwardPE", "pegRatio", "trailingPegRatio", "priceToBook", "priceToSalesTrailing12Months",
    "enterpriseToEbitda", "enterpriseToRevenue", "profitMargins", "grossMargins", "operatingMargins",
    "returnOnEquity", "returnOnAssets", "debtToEquity", "currentRatio", "revenueGrowth", "earningsGrowth",
    "dividendYield", "payoutRatio", "beta", "heldPercentInsiders", "heldPercentInstitutions",
    "shortPercentOfFloat", "recommendationMean", "numberOfAnalystOpinions", "targetMeanPrice",
    "freeCashflow", "operatingCashflow", "totalDebt", "totalCash", "ebitda", "totalRevenue",
]


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(label).lower())


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def normalize_statement(df: Optional[pd.DataFrame]) -> Dict[str, Dict[str, float]]:
    """
    yfinance statement (rows = line items, columns = period end dates) →
    {period_iso: {canonical_item: value}}, periods in ascending order.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    by_label = {_norm(idx): idx for idx in df.index}
    out: Dict[str, Dict[str, float]] = {}
    for col in sorted(df.columns, key=lambda c: pd.Timestamp(c)):
        period = pd.Timestamp(col).date().isoformat()
        row: Dict[str, float] = {}
        for item, candidates in STATEMENT_ITEMS.items():
            for cand in candidates:
                label = by_label.get(_norm(cand))
                if label is not None:
                    val = _num(df.at[label, col])
                    if val is not None:
                        row[item] = val
                        break
        if row:
            out[period] = row
    return out


def _df_records(df: Optional[pd.DataFrame], limit: int = 12) -> List[dict]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    df = df.reset_index().head(limit)
    records = []
    for rec in df.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if isinstance(v, (pd.Timestamp,)):
                clean[str(k)] = v.date().isoformat()
            else:
                n = _num(v)
                clean[str(k)] = n if n is not None else (None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v))
        records.append(clean)
    return records


def _date_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
        if v is None:
            return None
    try:
        return pd.Timestamp(v).date().isoformat()
    except Exception:
        return None


def _date_range(v: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Yahoo reports an unconfirmed earnings date as a [start, end] estimate window
    and a confirmed one as a single date. Returns (start, end); end == start for
    a single date, (None, None) when unparseable.
    """
    values = v if isinstance(v, (list, tuple)) else [v]
    dates = sorted(d for d in (_date_str(x) for x in values if x is not None) if d is not None)
    if not dates:
        return None, None
    return dates[0], dates[-1]


def failed_sections(snapshot: Dict[str, Any]) -> List[str]:
    """
    Sections whose fetch raised (rate limit, network, parse error) as opposed to
    sections Yahoo simply has no data for. Older snapshots only carry
    `data_gaps` strings of the form "section: ErrorType", so fall back to those.
    """
    errors = snapshot.get("fetch_errors")
    if isinstance(errors, list):
        return [str(e.get("section")) for e in errors if isinstance(e, dict) and e.get("section")]
    out = []
    for gap in snapshot.get("data_gaps") or []:
        m = re.fullmatch(r"([a-z_ ]+): ([A-Za-z_][\w.]*)", str(gap))
        if m:
            out.append(m.group(1))
    return out


def fetch_snapshot_sync(symbol: str) -> Dict[str, Any]:
    """Blocking: call from a worker thread."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    gaps: List[str] = []
    errors: List[Dict[str, str]] = []  # sections that FAILED (vs. merely empty)

    def attempt(name: str, fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - every section is optional
            gaps.append(f"{name}: {type(e).__name__}")
            errors.append({"section": name, "error": type(e).__name__})
            return default

    info = attempt("info", lambda: t.info or {}, {}) or {}
    fast = attempt("fast_info", lambda: t.fast_info, None)

    def fast_get(key):
        try:
            return fast.get(key) if fast is not None and hasattr(fast, "get") else None
        except Exception:
            return None

    price = _num(fast_get("lastPrice")) or _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    market_cap = _num(fast_get("marketCap")) or _num(info.get("marketCap"))
    shares = _num(fast_get("shares")) or _num(info.get("sharesOutstanding"))

    statements = {
        "annual": {
            "income": normalize_statement(attempt("income_stmt", lambda: t.get_income_stmt(pretty=False))),
            "balance": normalize_statement(attempt("balance_sheet", lambda: t.get_balance_sheet(pretty=False))),
            "cashflow": normalize_statement(attempt("cash_flow", lambda: t.get_cash_flow(pretty=False))),
        },
        "quarterly": {
            "income": normalize_statement(attempt("q_income_stmt", lambda: t.get_income_stmt(pretty=False, freq="quarterly"))),
            "cashflow": normalize_statement(attempt("q_cash_flow", lambda: t.get_cash_flow(pretty=False, freq="quarterly"))),
        },
    }
    for kind, data in statements["annual"].items():
        if not data:
            gaps.append(f"annual {kind} statement unavailable")

    targets = attempt("analyst_price_targets", lambda: t.analyst_price_targets, {}) or {}
    rec_summary = _df_records(attempt("recommendations_summary", lambda: t.recommendations_summary), 4)
    upgrades = _df_records(attempt("upgrades_downgrades", lambda: t.upgrades_downgrades), 10)
    earnings_hist = _df_records(attempt("earnings_history", lambda: t.earnings_history), 8)
    growth_est = _df_records(attempt("growth_estimates", lambda: t.growth_estimates), 8)
    rev_est = _df_records(attempt("revenue_estimate", lambda: t.revenue_estimate), 4)
    eps_est = _df_records(attempt("earnings_estimate", lambda: t.earnings_estimate), 4)
    insider = _df_records(attempt("insider_transactions", lambda: t.insider_transactions), 10)
    calendar = attempt("calendar", lambda: t.calendar, {}) or {}
    if not isinstance(calendar, dict):
        calendar = {}
    earnings_start, earnings_end = _date_range(calendar.get("Earnings Date"))

    # Price/market cap are quoted in the trading currency; statements are in the
    # reporting currency. They differ for ADRs and many cross-listings (e.g. TSM:
    # USD per ADR vs. TWD statements), and must never be mixed without FX.
    trading_currency = info.get("currency") or fast_get("currency") or None
    financial_currency = info.get("financialCurrency") or trading_currency

    summary = (info.get("longBusinessSummary") or "")[:2000]
    return {
        "symbol": symbol,
        "profile": {
            "name": info.get("longName") or info.get("shortName") or symbol,
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "country": info.get("country") or "",
            # `currency` is the statement (reporting) currency, kept for compatibility.
            "currency": financial_currency or "USD",
            "trading_currency": trading_currency,
            "financial_currency": financial_currency,
            "employees": info.get("fullTimeEmployees"),
            "website": info.get("website") or "",
            "summary": summary,
        },
        "market": {
            "price": price,
            "market_cap": market_cap,
            "shares_outstanding": shares,
            "beta": _num(info.get("beta")),
            "year_high": _num(fast_get("yearHigh")) or _num(info.get("fiftyTwoWeekHigh")),
            "year_low": _num(fast_get("yearLow")) or _num(info.get("fiftyTwoWeekLow")),
            "dividend_yield": _num(info.get("dividendYield")),
        },
        "info_metrics": {k: _num(info.get(k)) for k in INFO_FIELDS if _num(info.get(k)) is not None},
        "statements": statements,
        "street": {
            "price_targets": {k: _num(v) for k, v in targets.items()} if isinstance(targets, dict) else {},
            "recommendations": rec_summary,
            "upgrades_downgrades": upgrades,
            "earnings_history": earnings_hist,
            "growth_estimates": growth_est,
            "revenue_estimate": rev_est,
            "earnings_estimate": eps_est,
        },
        "insider_transactions": insider,
        "calendar": {
            "next_earnings": earnings_start,
            "next_earnings_end": earnings_end,
            "ex_dividend": _date_str(calendar.get("Ex-Dividend Date")),
        },
        "data_gaps": gaps,
        "fetch_errors": errors,
    }
