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


def make_snapshot(symbol="SYN", price=75.0, sector="Technology", **kw):
    inc, bal, cf = yf_frames(**kw)
    return {
        "symbol": symbol,
        "profile": {"name": f"{symbol} Corp", "sector": sector, "industry": "Software", "summary": "Makes things."},
        "market": {"price": price, "market_cap": price * 4.85e9, "beta": 1.1, "shares_outstanding": 4.85e9},
        "info_metrics": {"recommendationMean": 2.0},
        "street": {"price_targets": {"mean": price * 1.12, "low": price * 0.8, "high": price * 1.45},
                   "revenue_estimate": [{"period": "+1y", "growth": 0.07}],
                   "earnings_history": [{"surprisePercent": 0.03}, {"surprisePercent": 0.01}]},
        "insider_transactions": [],
        "calendar": {"next_earnings": "2030-01-30", "ex_dividend": None},
        "statements": {"annual": {"income": normalize_statement(inc), "balance": normalize_statement(bal),
                                  "cashflow": normalize_statement(cf)}},
        "data_gaps": [],
    }
