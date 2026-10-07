"""Synthetic companies with yfinance-shaped statements (rows = items, newest column first)."""
import pandas as pd

from backend.research.data import normalize_statement


def yf_frames(rev0=100e9, growth=0.08, years=4, margin=0.19, fcf_margin=0.20, debt=40e9, distressed=False):
    cols = [pd.Timestamp(f"{2022 + i}-12-31") for i in range(years)][::-1]

    def series(fn):
        return {c: fn(years - 1 - k) for k, c in enumerate(cols)}

    def rev(i):
        return rev0 * (1 + growth) ** i

    retained = (lambda i: -30e9 - 5e9 * i) if distressed else (lambda i: 50e9 + 15e9 * i)
    inc = pd.DataFrame({
        "TotalRevenue": series(rev), "GrossProfit": series(lambda i: rev(i) * (0.42 + 0.01 * i)),
        "OperatingIncome": series(lambda i: rev(i) * (margin + 0.06)), "EBIT": series(lambda i: rev(i) * (margin + 0.06)),
        "EBITDA": series(lambda i: rev(i) * (margin + 0.11)), "NetIncomeCommonStockholders": series(lambda i: rev(i) * margin),
        "PretaxIncome": series(lambda i: rev(i) * (margin + 0.045)), "TaxProvision": series(lambda i: rev(i) * 0.045),
        "InterestExpense": series(lambda i: debt * 0.04), "DilutedEPS": series(lambda i: rev(i) * margin / 5e9),
        "DilutedAverageShares": series(lambda i: 5e9 * (1 - 0.01 * i)),
    }).T
    bal = pd.DataFrame({
        "TotalAssets": series(lambda i: 200e9 + 10e9 * i), "CurrentAssets": series(lambda i: 60e9 + 5e9 * i),
        "CurrentLiabilities": series(lambda i: 40e9 if not distressed else 90e9),
        "TotalLiabilitiesNetMinorityInterest": series(lambda i: 110e9 if not distressed else 190e9),
        "TotalDebt": series(lambda i: debt - 2e9 * i), "LongTermDebt": series(lambda i: debt * 0.75 - 2e9 * i),
        "CashAndCashEquivalents": series(lambda i: 25e9), "StockholdersEquity": series(lambda i: 90e9 + 10e9 * i),
        "RetainedEarnings": series(retained), "OrdinarySharesNumber": series(lambda i: 5e9 * (1 - 0.01 * i)),
    }).T
    cf = pd.DataFrame({
        "OperatingCashFlow": series(lambda i: rev(i) * (fcf_margin + 0.04)),
        "CapitalExpenditure": series(lambda i: -rev(i) * 0.04),
        "FreeCashFlow": series(lambda i: rev(i) * fcf_margin), "CashDividendsPaid": series(lambda i: -3e9),
        "RepurchaseOfCapitalStock": series(lambda i: -8e9), "StockBasedCompensation": series(lambda i: 2e9),
    }).T
    return inc, bal, cf


def make_snapshot(symbol="SYN", price=75.0, sector="Technology", industry=None, trading_currency="USD",
                  financial_currency="USD", **kw):
    inc, bal, cf = yf_frames(**kw)
    if industry is None:
        industry = "Banks - Diversified" if sector == "Financial Services" else "Software - Application"
    return {
        "symbol": symbol,
        "profile": {"name": f"{symbol} Corp", "sector": sector, "industry": industry, "summary": "Makes things.",
                    "currency": financial_currency, "trading_currency": trading_currency,
                    "financial_currency": financial_currency},
        "market": {"price": price, "market_cap": price * 4.85e9, "beta": 1.1, "shares_outstanding": 4.85e9},
        "info_metrics": {"recommendationMean": 2.0},
        "street": {"price_targets": {"mean": price * 1.12, "low": price * 0.8, "high": price * 1.45},
                   "revenue_estimate": [{"period": "+1y", "growth": 0.07}],
                   "earnings_history": [{"surprisePercent": 0.03}, {"surprisePercent": 0.01}]},
        "insider_transactions": [],
        "calendar": {"next_earnings": "2030-01-30", "next_earnings_end": "2030-01-30", "ex_dividend": None},
        "statements": {"annual": {"income": normalize_statement(inc), "balance": normalize_statement(bal),
                                  "cashflow": normalize_statement(cf)}},
        "data_gaps": [],
        "fetch_errors": [],
    }


def scale_item(snap, kind, item, factor):
    """Multiply one canonical statement item in every annual period (e.g. to make a lender asset-heavy)."""
    for row in snap["statements"]["annual"][kind].values():
        if item in row:
            row[item] *= factor
    return snap


class FakeTicker:
    """yfinance.Ticker stand-in for fetch_snapshot_sync: `fail` names sections that raise."""

    def __init__(self, symbol, info=None, fast=None, calendar=None, fail=(), empty=(), **frames_kw):
        self.symbol = symbol
        self._info = info if info is not None else {"longName": f"{symbol} Inc", "sector": "Technology",
                                                    "industry": "Software - Application", "currency": "USD",
                                                    "financialCurrency": "USD", "beta": 1.1}
        self._fast = fast if fast is not None else {"lastPrice": 75.0, "marketCap": 75.0 * 4.85e9, "shares": 4.85e9}
        self._calendar = calendar if calendar is not None else {}
        self._fail, self._empty = set(fail), set(empty)
        self._inc, self._bal, self._cf = yf_frames(**frames_kw)

    def _section(self, name, value):
        if name in self._fail:
            raise RuntimeError(f"{name} rate limited")  # stands in for YFRateLimitError / network errors
        return pd.DataFrame() if name in self._empty and isinstance(value, pd.DataFrame) else value

    @property
    def info(self):
        return self._section("info", self._info)

    @property
    def fast_info(self):
        return self._section("fast_info", self._fast)

    def get_income_stmt(self, pretty=False, freq="yearly"):
        if freq == "quarterly":
            return self._section("q_income_stmt", pd.DataFrame())
        return self._section("income_stmt", self._inc)

    def get_balance_sheet(self, pretty=False, freq="yearly"):
        return self._section("balance_sheet", self._bal)

    def get_cash_flow(self, pretty=False, freq="yearly"):
        if freq == "quarterly":
            return self._section("q_cash_flow", pd.DataFrame())
        return self._section("cash_flow", self._cf)

    @property
    def calendar(self):
        return self._section("calendar", self._calendar)

    @property
    def analyst_price_targets(self):
        return self._section("analyst_price_targets", {"mean": 84.0, "low": 60.0, "high": 110.0})

    def __getattr__(self, name):  # remaining analysis tables: empty frames
        if name in ("recommendations_summary", "upgrades_downgrades", "earnings_history", "growth_estimates",
                    "revenue_estimate", "earnings_estimate", "insider_transactions"):
            return self._section(name, pd.DataFrame())
        raise AttributeError(name)
