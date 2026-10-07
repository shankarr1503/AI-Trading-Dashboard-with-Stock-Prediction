import asyncio
import sys
import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import cache
from backend.config import settings
from backend.research import scorecard as sc
from backend.research.analyst import AnalystError, ClaudeAnalyst, add_usage, empty_usage, quant_report, validate_report
from backend.research.data import _date_range, failed_sections, fetch_snapshot_sync, normalize_statement
from backend.research.fundamentals import analyze_fundamentals, classify_business, merge_annual
from backend.research.service import DEGRADED_SNAPSHOT_TTL, SNAPSHOT_TTL, ResearchService
from backend.research.valuation import dcf_enterprise_value, implied_growth, value_company
from tests.conftest import FakeMarket, make_ohlcv, make_trending
from tests.research_fixtures import FakeTicker, make_snapshot, scale_item, yf_frames


def test_statement_normalisation_orders_periods_and_maps_labels():
    inc, _, _ = yf_frames()
    out = normalize_statement(inc)
    assert list(out) == sorted(out)                       # ascending periods
    assert out["2025-12-31"]["revenue"] > out["2022-12-31"]["revenue"]
    pretty = inc.rename(index={"TotalRevenue": "Total Revenue"})
    assert normalize_statement(pretty)["2025-12-31"]["revenue"] == out["2025-12-31"]["revenue"]


def test_fundamentals_quality_company():
    f = analyze_fundamentals(make_snapshot())
    m = f["metrics"]
    assert m["revenue_cagr"] == pytest.approx(0.08, abs=1e-6)
    assert 0.15 < m["roic"] < 0.30
    assert f["piotroski"]["score"] >= 7
    assert f["altman"]["zone"] == "safe"
    assert f["flags"] == []


def test_fundamentals_flags_distress():
    f = analyze_fundamentals(make_snapshot(distressed=True, margin=0.01, fcf_margin=-0.02, growth=-0.10, debt=120e9))
    assert f["altman"]["zone"] == "distress"
    assert any("Altman" in x for x in f["flags"])
    assert any("Negative free cash flow" in x for x in f["flags"])


def test_dcf_math():
    ev_lo = dcf_enterprise_value(100, 0.02, 0.025, 0.09)
    ev_hi = dcf_enterprise_value(100, 0.10, 0.025, 0.09)
    assert ev_hi > ev_lo > 0
    g = implied_growth(ev_hi, 100, 0.025, 0.09)
    assert g == pytest.approx(0.10, abs=1e-4)              # reverse DCF inverts the forward DCF
    with pytest.raises(ValueError):
        dcf_enterprise_value(100, 0.05, 0.08, 0.085)


def test_valuation_scenarios_and_methods():
    snap = make_snapshot()
    v = value_company(snap, analyze_fundamentals(snap))
    s = v["scenarios"]
    assert v["method"] == "dcf"
    assert s["bear"]["value"] < s["base"]["value"] < s["bull"]["value"]
    assert sum(x["probability"] for x in s.values()) == pytest.approx(1.0)
    assert v["fair_value"] and v["market_implied_growth"] is not None

    neg = make_snapshot(fcf_margin=-0.05)
    vn = value_company(neg, analyze_fundamentals(neg))
    assert vn["method"] == "none" and vn["fair_value"] is None and vn["warnings"]

    bank = make_snapshot(sector="Financial Services")
    vb = value_company(bank, analyze_fundamentals(bank))
    assert vb["method"] == "justified_pb" and vb["fair_value"] > 0


def test_scorecard_bounds_and_financial_exclusions():
    snap = make_snapshot()
    f = analyze_fundamentals(snap)
    card = sc.score(f, value_company(snap, f), snap, sc.price_stats(make_trending(1)))
    assert 0 <= card["composite"] <= 100
    for factor in card["factors"].values():
        assert factor["score"] is None or 0 <= factor["score"] <= 100
    assert "price_to_book" not in card["factors"]["value"]["inputs"]
    bank = make_snapshot(sector="Financial Services")
    fb = analyze_fundamentals(bank)
    cb = sc.score(fb, value_company(bank, fb), bank, {})
    assert "ev_ebitda" not in cb["factors"]["value"]["inputs"] and "price_to_book" in cb["factors"]["value"]["inputs"]


def test_rank_universe_percentiles():
    rows = [{"symbol": s, "scores": {"composite": c}} for s, c in (("A", 40), ("B", 80), ("C", 60))]
    ranked = sc.rank_universe(rows)
    assert [r["symbol"] for r in ranked] == ["B", "C", "A"]
    assert ranked[0]["percentiles"]["composite"] == 100 and ranked[-1]["percentiles"]["composite"] == 0


def _case(t, p):
    return {"price_target": t, "probability": p, "narrative": "x"}


def base_report(**kw):
    r = {"rating": "BUY", "conviction": 4, "summary": "s", "thesis": ["t"],
         "bear_case": _case(60, 0.25), "base_case": _case(90, 0.5), "bull_case": _case(120, 0.25),
         "catalysts": [], "risks": [], "moat": "wide", "moat_rationale": "", "financial_health": "",
         "valuation_view": "", "technical_view": "", "what_would_change_our_mind": [], "data_gaps": []}
    r.update(kw)
    return r


def test_validate_report():
    rep = validate_report(base_report(bear_case=_case(60, 0.3), base_case=_case(90, 0.6), bull_case=_case(120, 0.3)), 75.0)
    assert sum(rep[k]["probability"] for k in ("bear_case", "base_case", "bull_case")) == pytest.approx(1.0)
    assert rep["expected_price"] == pytest.approx(90.0, abs=0.01)
    assert rep["consistency_warnings"] == []
    with pytest.raises(ValueError):
        validate_report(base_report(bear_case=_case(130, 0.25)), 75.0)   # bear above base
    with pytest.raises(ValueError):
        validate_report(base_report(rating="MOON"), 75.0)
    assert validate_report(base_report(rating="BUY", bear_case=_case(30, 0.25), base_case=_case(40, 0.5),
                                       bull_case=_case(50, 0.25)), 75.0)["consistency_warnings"]


def dossier_for(snap, df):
    f = analyze_fundamentals(snap)
    v = value_company(snap, f)
    return {"symbol": snap["symbol"], "snapshot": snap, "fundamentals": f, "valuation": v,
            "scorecard": sc.score(f, v, snap, sc.price_stats(df)), "technical": {}}


def test_quant_report_ratings():
    good = quant_report(dossier_for(make_snapshot(price=60.0), make_trending(1)))
    assert good["rating"] in ("BUY", "STRONG_BUY")
    bad = quant_report(dossier_for(make_snapshot(price=20.0, distressed=True, margin=0.01, fcf_margin=-0.02,
                                                 growth=-0.10, debt=120e9), make_ohlcv(3)))
    assert bad["rating"] in ("SELL", "STRONG_SELL")
    assert bad["risks"]


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def tool_use(name, inp, id_):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=id_)


async def test_claude_analyst_loop():
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("get_financials", {}, "a"), tool_use("get_valuation", {}, "b")],
                        model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=1000, output_tokens=200)),
        SimpleNamespace(stop_reason="pause_turn", content=[], model="claude-opus-5-5", usage=None),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("submit_report", base_report(), "c")],
                        model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=3000, output_tokens=900)),
    ]
    msgs = FakeMessages(responses)
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    seen = []

    async def handler(name, args):
        seen.append(name)
        return {"ok": name}

    rep = await ClaudeAnalyst(client=client, web_search=True).write_report("SYN", 75.0, handler)
    assert rep["rating"] == "BUY"
    assert rep["usage"]["input_tokens"] == 4000 and rep["usage"]["output_tokens"] == 1100
    assert rep["usage"]["api_calls"] == 3
    assert seen == ["get_financials", "get_valuation"]
    first = msgs.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert any(t.get("type") == "web_search_20260209" for t in first["tools"])
    # The message list is shared across calls, so inspect the final transcript.
    transcript = msgs.calls[-1]["messages"]
    results = [m for m in transcript if m["role"] == "user" and isinstance(m["content"], list)]
    assert [r["tool_use_id"] for r in results[0]["content"]] == ["a", "b"]   # all results in one user message


@pytest.fixture
def research(monkeypatch):
    snaps = {"SYN": make_snapshot("SYN"), "BAD": make_snapshot("BAD", price=20.0, distressed=True, margin=0.01,
                                                               fcf_margin=-0.02, growth=-0.1, debt=120e9)}
    market = FakeMarket({"SYN": make_trending(1), "BAD": make_ohlcv(5)})

    def fetch(sym):
        if sym not in snaps:
            raise ValueError("unknown")
        return snaps[sym]

    svc = ResearchService(market=market, snapshot_fetcher=fetch)
    import backend.research.router as rr
    monkeypatch.setattr(rr, "research_service", svc)
    return svc


async def test_service_report_is_persisted_and_cached(db_tables, research):
    from backend.database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        first = await research.report(db, "SYN", allow_llm=False)
        await db.commit()
    async with AsyncSessionLocal() as db:
        again = await research.report(db, "SYN", allow_llm=False)
    assert first["id"] == again["id"] and first["source"] == "quant_model"
    view = await research.fundamental_view("BAD")
    assert view["available"] and view["altman_zone"] == "distress"


def _tokens(c):
    c.post("/auth/register", json={"email": "a@example.com", "username": "alice", "password": "correct-horse-1"})
    c.post("/auth/register", json={"email": "b@example.com", "username": "bob", "password": "correct-horse-1"})
    out = []
    for email in ("a@example.com", "b@example.com"):   # alice: first user → admin (test env); bob: regular user
        tok = c.post("/auth/login", json={"email": email, "password": "correct-horse-1"}).json()["access_token"]
        out.append({"Authorization": f"Bearer {tok}"})
    return out


def test_research_api(db_tables, research, monkeypatch):
    from backend.main import app
    import backend.research.router as rr

    monkeypatch.setitem(rr.UNIVERSES, "test_universe", ["SYN", "BAD"])
    with TestClient(app) as c:
        h_admin, h = _tokens(c)
        assert c.get("/api/research/SYN/fundamentals").status_code == 401
        d = c.get("/api/research/SYN/fundamentals", headers=h).json()
        assert d["scorecard"]["composite"] is not None and "statements" not in d["snapshot"]
        rep = c.get("/api/research/SYN/report", headers=h).json()
        assert rep["source"] == "quant_model" and rep["rating"] in ("STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL")
        # Admins may screen anything.
        scr = c.post("/api/research/screen", headers=h_admin, json={"symbols": ["SYN", "BAD", "NOPE"]}).json()
        assert [r["symbol"] for r in scr["results"]][:2] == ["SYN", "BAD"] and "error" in scr["results"][2]
        # Regular users: only symbols from the predefined universes.
        assert c.post("/api/research/screen", headers=h, json={"symbols": ["SYN", "BAD"]}).status_code == 200
        assert c.post("/api/research/screen", headers=h, json={"universe": "test_universe"}).status_code == 200
        denied = c.post("/api/research/screen", headers=h, json={"symbols": ["SYN", "NOPE"]})
        assert denied.status_code == 403 and "NOPE" in denied.json()["detail"]
        assert c.get("/api/research/reports", headers=h).json()[0]["symbol"] == "SYN"


# ─── Regression tests for the verified research findings ─────────────────────

VIEW_KEYS = {"available", "composite", "coverage", "altman_zone", "altman_model", "piotroski", "flags", "fair_value",
             "upside_pct", "next_earnings", "next_earnings_end", "earnings_unknown", "data_degraded"}


# 1. Payment networks (Yahoo sector "Financial Services") are not banks.
def test_payment_network_in_financial_sector_is_valued_by_dcf_not_book():
    for industry in ("Credit Services", ""):          # Visa/Mastercard; and an unknown industry in that sector
        snap = make_snapshot("PAY", sector="Financial Services", industry=industry, margin=0.45, fcf_margin=0.5)
        f = analyze_fundamentals(snap)
        assert f["business_model"]["type"] == "asset_light_financial" and not f["is_financial"]
        v = value_company(snap, f)
        assert v["method"] == "dcf"
        s = v["scenarios"]
        assert s["bear"]["value"] < s["base"]["value"] < s["bull"]["value"]
        assert f["altman"]["model"] == "z_double_prime"         # Altman applies, non-manufacturer variant
        card = sc.score(f, v, snap, {})
        assert "gross_margin" in card["factors"]["quality"]["inputs"]
        assert "price_to_book" not in card["factors"]["value"]["inputs"]


def test_lenders_insurers_and_brokers_classification():
    lender = scale_item(make_snapshot("CARD", sector="Financial Services", industry="Credit Services"),
                        "balance", "total_assets", 6)        # card lender: loan book → assets ~11x revenue
    assert classify_business("Financial Services", "Credit Services",
                             merge_annual(lender)["2025-12-31"])["is_financial"]
    assert analyze_fundamentals(lender)["altman"]["zone"] == "not_applicable"
    for industry in ("Banks—Regional", "Banks - Diversified", "Insurance - Life", "Capital Markets", "Mortgage Finance"):
        assert classify_business("Financial Services", industry, {})["is_financial"], industry
    broker = merge_annual(make_snapshot())["2025-12-31"]
    assert not classify_business("Financial Services", "Insurance Brokers", broker)["is_financial"]
    assert not classify_business("Technology", "Software - Infrastructure", {})["is_financial"]


def test_justified_pb_outside_validity_band_is_not_applicable_instead_of_collapsed():
    # A bank whose ROE implies ~6.8x book: the old 4x clamp reported bear = base = bull.
    snap = make_snapshot("HIROE", sector="Financial Services", industry="Banks - Regional", margin=0.45, fcf_margin=0.5)
    f = analyze_fundamentals(snap)
    assert f["is_financial"]
    v = value_company(snap, f)
    assert v["method"] == "none" and v["fair_value"] is None and v["scenarios"] == {} and v["upside_pct"] is None
    assert any("not applicable" in w for w in v["warnings"])
    bank = make_snapshot("BNK", sector="Financial Services")
    vb = value_company(bank, analyze_fundamentals(bank))
    values = [vb["scenarios"][k]["value"] for k in ("bear", "base", "bull")]
    assert vb["method"] == "justified_pb" and values == sorted(values) and len(set(values)) == 3


# 2. Altman: model by sector; utilities/REITs not applicable (no distress veto).
async def test_altman_model_selection_and_no_distress_for_utilities_and_reits():
    def altman(**kw):
        return analyze_fundamentals(make_snapshot(**kw))["altman"]

    assert altman(sector="Technology")["model"] == "z_double_prime"
    assert altman(sector="Industrials", industry="Specialty Industrial Machinery")["model"] == "z"
    assert altman(sector="Industrials", industry="Airlines")["model"] == "z_double_prime"
    assert altman(sector="Consumer Cyclical", industry="Restaurants")["model"] == "z_double_prime"
    assert altman(sector="Consumer Cyclical", industry="Auto Manufacturers")["model"] == "z"
    assert altman(sector="Industrials", industry="Farm & Heavy Construction Machinery",
                  financial_currency="EUR")["model"] == "z_prime"    # market cap in another currency
    assert altman(sector="Financial Services")["zone"] == "not_applicable"

    # Z'' value: 6.56·WC/TA + 3.26·RE/TA + 6.72·EBIT/TA + 1.05·BookEquity/TL
    snap = make_snapshot(sector="Technology")
    p = merge_annual(snap)["2025-12-31"]
    ta, tl = p["total_assets"], p["total_liabilities"]
    expected = (6.56 * (p["current_assets"] - p["current_liabilities"]) / ta + 3.26 * p["retained_earnings"] / ta
                + 6.72 * p["ebit"] / ta + 1.05 * p["equity"] / tl)
    z = analyze_fundamentals(snap)["altman"]
    assert z["z"] == pytest.approx(expected, abs=1e-3) and z["zone"] == "safe"

    snaps = {sym: make_snapshot(sym, sector=sector, industry=industry, distressed=True, margin=0.12)
             for sym, sector, industry in (("UTIL", "Utilities", "Utilities - Regulated Electric"),
                                           ("REIT", "Real Estate", "REIT - Retail"))}
    svc = ResearchService(market=FakeMarket({s: make_trending(1) for s in snaps}), snapshot_fetcher=snaps.__getitem__)
    for sym in snaps:
        f = analyze_fundamentals(snaps[sym])
        assert f["altman"]["zone"] == "not_applicable" and not any("Altman" in x for x in f["flags"])
        view = await svc.fundamental_view(sym)
        assert view["altman_zone"] == "not_applicable" and view["altman_model"] is None   # no bot veto
    tech = analyze_fundamentals(make_snapshot(distressed=True, margin=0.01, fcf_margin=-0.02, growth=-0.10, debt=120e9))
    assert tech["altman"]["zone"] == "distress" and any("Altman Z''" in x for x in tech["flags"])


# 3. DCF discounts FCFF (FCF + after-tax interest) at WACC, then subtracts net debt.
def test_dcf_discounts_fcff_at_wacc():
    inc, _, _ = yf_frames()
    pretty = normalize_statement(inc.rename(index={"InterestExpense": "Interest Expense"}))
    assert pretty["2025-12-31"]["interest_expense"] == pytest.approx(40e9 * 0.04)

    snap = make_snapshot("LEV", debt=150e9, fcf_margin=0.12)
    f = analyze_fundamentals(snap)
    p = merge_annual(snap)["2025-12-31"]
    t = p["tax"] / p["pretax_income"]
    assert f["history"][-1]["fcff"] == pytest.approx(p["fcf"] + p["interest_expense"] * (1 - t))
    assert f["history"][-1]["fcff"] > f["history"][-1]["fcf"]

    v = value_company(snap, f)
    fcffs = [h["fcff"] for h in f["history"]][-3:]
    base = 0.5 * fcffs[-1] + 0.5 * sum(fcffs) / 3
    assert v["fcff_base"] == pytest.approx(base, rel=1e-9)
    sc_base = v["scenarios"]["base"]
    net_debt = p["total_debt"] - p["cash"]
    expected = (dcf_enterprise_value(base, sc_base["growth"], 0.025, sc_base["wacc"]) - net_debt) / v["shares"]
    assert sc_base["value"] == pytest.approx(expected, abs=0.01)
    # Reverse DCF inverts the same FCFF/EV relationship.
    assert v["market_implied_growth"] == pytest.approx(
        implied_growth(v["enterprise_value"], base, 0.025, v["cost_of_capital"]["wacc"]), abs=1e-4)


# 4. ADR / cross-listing: statement currency ≠ trading currency → no mixed-unit ratios.
def test_currency_mismatch_disables_mixed_unit_valuation_and_strong_ratings():
    same = make_snapshot("ADR", price=10.0)                      # very cheap: STRONG_BUY when units agree
    adr = make_snapshot("ADR", price=10.0, financial_currency="TWD")
    d_same, d_adr = dossier_for(same, make_trending(1)), dossier_for(adr, make_trending(1))
    assert quant_report(d_same)["rating"] == "STRONG_BUY"

    f, v, card = d_adr["fundamentals"], d_adr["valuation"], d_adr["scorecard"]
    assert f["currency"] == {"trading": "USD", "financial": "TWD", "mismatch": True}
    for k in ("pe", "forward_pe", "ev_ebitda", "ev_sales", "price_to_fcf", "fcf_yield", "earnings_yield", "price_to_book"):
        assert v["multiples"][k] is None, k
    assert v["enterprise_value"] is None and v["method"] == "none" and v["fair_value"] is None
    assert v["upside_pct"] is None and v["market_implied_growth"] is None
    assert any("TWD" in w and "USD" in w for w in v["warnings"])
    assert f["metrics"]["shareholder_yield"] is None
    assert card["factors"]["value"]["score"] is None
    assert "fcf_yield" in card["factors"]["value"]["inputs"]       # unscored, still counted
    assert card["coverage"] < d_same["scorecard"]["coverage"] and card["notes"]
    rep = quant_report(d_adr)
    assert rep["rating"] != "STRONG_BUY" and any("TWD" in g for g in rep["data_gaps"])
    # Even with stellar quality/growth/street scores, no valuation anchor → no STRONG call.
    d_adr["scorecard"]["composite"] = 98.0
    d_adr["valuation"]["street"]["upside_to_mean_pct"] = 60.0
    rep = quant_report(d_adr)
    assert rep["rating"] == "BUY" and any("No valuation input" in g for g in rep["data_gaps"])


# 5. Dossier price comes from the fresh daily close, not the 12h snapshot.
async def test_dossier_uses_fresh_close_not_cached_snapshot_price():
    df = make_trending(1)
    close = float(df["close"].iloc[-1])
    snap = make_snapshot("SYN", price=close * 1.4)               # captured before a -29% gap
    svc = ResearchService(market=FakeMarket({"SYN": df}), snapshot_fetcher=lambda s: snap)
    d = await svc.dossier("SYN")
    market = d["snapshot"]["market"]
    assert d["valuation"]["price"] == pytest.approx(close) and market["price_source"] == "last_close"
    assert market["market_cap"] == pytest.approx(4.85e9 * close)
    assert d["valuation"]["street"]["upside_to_mean_pct"] == pytest.approx((close * 1.4 * 1.12 / close - 1) * 100, abs=0.01)
    assert snap["market"]["price"] == pytest.approx(close * 1.4)   # cached snapshot not mutated


# 6. A caller timeout neither cancels nor repeats the in-flight snapshot fetch.
async def test_caller_timeout_does_not_cancel_or_repeat_the_snapshot_fetch():
    calls, release = [], threading.Event()

    def slow(sym):
        calls.append(sym)
        release.wait(5)
        return make_snapshot(sym)

    svc = ResearchService(market=FakeMarket({"SYN": make_trending(1)}), snapshot_fetcher=slow)
    try:
        for _ in range(3):                                         # the bot's wait_for(…, 30) budget expiring
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(svc.fundamental_view("SYN"), 0.05)
        assert calls == ["SYN"]                                   # one shared in-flight fetch
    finally:
        release.set()
    for _ in range(200):
        if await cache.cache_get("research:snapshot:SYN") is not None:
            break
        await asyncio.sleep(0.01)
    assert await cache.cache_get("research:snapshot:SYN") is not None   # result cached on completion
    assert (await svc.fundamental_view("SYN"))["available"] and calls == ["SYN"]

    fast_calls = []
    svc2 = ResearchService(market=FakeMarket({}), snapshot_fetcher=lambda s: fast_calls.append(s) or make_snapshot(s))
    await asyncio.gather(*(svc2.snapshot("DUP") for _ in range(5)))
    assert fast_calls == ["DUP"]                                  # concurrent callers de-duplicated


# 7. A snapshot with a FAILED section is cached briefly and the earnings date is flagged unknown.
async def test_degraded_snapshot_cached_briefly_and_flags_unknown_earnings():
    def fetch(sym):
        s = make_snapshot(sym)
        if sym == "SYN":
            s["calendar"] = {"next_earnings": None, "next_earnings_end": None, "ex_dividend": None}
            s["fetch_errors"] = [{"section": "calendar", "error": "YFRateLimitError"}]
            s["data_gaps"] = ["calendar: YFRateLimitError"]
        if sym == "NOCAL":                                         # calendar empty, but fetched fine
            s["calendar"] = {"next_earnings": None, "next_earnings_end": None, "ex_dividend": None}
            s["data_gaps"] = ["annual cashflow statement unavailable"]
        return s

    svc = ResearchService(market=FakeMarket({s: make_trending(1) for s in ("SYN", "OK", "NOCAL")}), snapshot_fetcher=fetch)
    view = await svc.fundamental_view("SYN")
    assert view["available"] and view["earnings_unknown"] is True and view["data_degraded"] is True
    assert view["next_earnings"] is None
    now = time.monotonic()
    assert 0 < cache._local["research:snapshot:SYN"][0] - now <= DEGRADED_SNAPSHOT_TTL
    assert cache._local["research:dossier:SYN"][0] - now <= DEGRADED_SNAPSHOT_TTL

    for sym in ("OK", "NOCAL"):
        v = await svc.fundamental_view(sym)
        assert v["earnings_unknown"] is False and v["data_degraded"] is False
        assert cache._local[f"research:snapshot:{sym}"][0] - time.monotonic() > SNAPSHOT_TTL - 60
    # Legacy snapshots without fetch_errors: errors are recognised from data_gaps.
    assert failed_sections({"data_gaps": ["calendar: YFRateLimitError", "annual income statement unavailable"]}) == ["calendar"]


def test_fetch_snapshot_records_currencies_errors_and_earnings_window(monkeypatch):
    def run(**kw):
        monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda s: FakeTicker(s, **kw)))
        return fetch_snapshot_sync("TSM")

    info = {"longName": "Taiwan Semi", "sector": "Technology", "industry": "Semiconductors",
            "currency": "USD", "financialCurrency": "TWD"}
    cal = {"Earnings Date": [date(2026, 11, 3), date(2026, 10, 28)], "Ex-Dividend Date": date(2026, 9, 1)}
    snap = run(info=info, calendar=cal, fail={"earnings_history"}, empty={"q_income_stmt"})
    assert snap["profile"]["trading_currency"] == "USD" and snap["profile"]["financial_currency"] == "TWD"
    assert snap["calendar"]["next_earnings"] == "2026-10-28" and snap["calendar"]["next_earnings_end"] == "2026-11-03"
    assert failed_sections(snap) == ["earnings_history"]          # empty sections are not failures
    assert analyze_fundamentals(snap)["currency"]["mismatch"]

    snap = run(calendar={"Earnings Date": [date(2026, 10, 28)]})
    assert snap["calendar"]["next_earnings"] == snap["calendar"]["next_earnings_end"] == "2026-10-28"
    assert snap["fetch_errors"] == [] and not analyze_fundamentals(snap)["currency"]["mismatch"]

    snap = run(fail={"calendar"})
    assert snap["calendar"]["next_earnings"] is None and failed_sections(snap) == ["calendar"]


# 8. Earnings window: both ends exposed to the bot.
async def test_fundamental_view_exposes_earnings_window():
    assert _date_range([date(2026, 11, 3), date(2026, 10, 28)]) == ("2026-10-28", "2026-11-03")
    assert _date_range(date(2026, 10, 28)) == ("2026-10-28", "2026-10-28")
    assert _date_range(None) == (None, None) and _date_range([]) == (None, None)

    ranged = make_snapshot("RNG")
    ranged["calendar"] = {"next_earnings": "2026-10-28", "next_earnings_end": "2026-11-03", "ex_dividend": None}
    legacy = make_snapshot("OLD")
    legacy["calendar"] = {"next_earnings": "2026-10-28", "ex_dividend": None}    # cached before the change
    snaps = {"RNG": ranged, "OLD": legacy}
    svc = ResearchService(market=FakeMarket({s: make_trending(1) for s in snaps}), snapshot_fetcher=snaps.__getitem__)
    view = await svc.fundamental_view("RNG")
    assert VIEW_KEYS <= set(view)
    assert (view["next_earnings"], view["next_earnings_end"]) == ("2026-10-28", "2026-11-03")
    old = await svc.fundamental_view("OLD")
    assert old["next_earnings"] == old["next_earnings_end"] == "2026-10-28"


# 9. A paid Claude run is not thrown away over an invalid submission, and failures back off.
def _llm_response(inp, id_, tokens=(1000, 200)):
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use("submit_report", inp, id_)], model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]))


def _client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


async def _noop_handler(name, args):
    return {}


async def test_invalid_submission_is_returned_to_the_model_for_correction():
    bad = base_report(bear_case=_case(130, 0.25))                  # bear above base
    msgs = FakeMessages([_llm_response(bad, "s1", (500, 100)), _llm_response(base_report(), "s2", (700, 120))])
    rep = await ClaudeAnalyst(client=_client(msgs), web_search=False).write_report("SYN", 75.0, _noop_handler)
    assert rep["rating"] == "BUY" and rep["usage"]["input_tokens"] == 1200 and rep["usage"]["api_calls"] == 2
    err = msgs.calls[-1]["messages"][2]["content"][0]
    assert err["tool_use_id"] == "s1" and err["is_error"] is True and "bear" in err["content"]

    msgs = FakeMessages([_llm_response(bad, f"s{i}", (500, 100)) for i in range(3)])
    with pytest.raises(AnalystError) as ei:
        await ClaudeAnalyst(client=_client(msgs), web_search=False).write_report("SYN", 75.0, _noop_handler)
    assert ei.value.usage["input_tokens"] == 1500 and ei.value.usage["api_calls"] == 3


async def test_api_error_mid_run_keeps_partial_usage():
    class Boom:
        def __init__(self):
            self.n = 0

        async def create(self, **kw):
            self.n += 1
            if self.n == 1:
                return SimpleNamespace(stop_reason="tool_use", content=[tool_use("get_financials", {}, "a")],
                                       model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=900, output_tokens=80))
            raise RuntimeError("overloaded")

    with pytest.raises(AnalystError) as ei:
        await ClaudeAnalyst(client=_client(Boom()), web_search=False).write_report("SYN", 75.0, _noop_handler)
    assert ei.value.usage["input_tokens"] == 900 and "overloaded" in str(ei.value)


class _CountingMessages:
    def __init__(self, report, delay=0.0):
        self.report, self.delay, self.calls = report, delay, 0

    async def create(self, **kw):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return _llm_response(self.report, f"s{self.calls}")


async def test_failed_claude_run_is_recorded_with_usage_and_backs_off(db_tables, monkeypatch):
    from backend.database.session import AsyncSessionLocal

    monkeypatch.setattr(settings, "RESEARCH_LLM_ENABLED", True)
    msgs = _CountingMessages(base_report(bear_case=_case(130, 0.25)))     # never valid
    market = FakeMarket({"SYN": make_trending(1)})
    svc = ResearchService(market=market, snapshot_fetcher=make_snapshot,
                          analyst=ClaudeAnalyst(client=_client(msgs), web_search=False))
    async with AsyncSessionLocal() as db:
        first = await svc.report(db, "SYN", allow_llm=True)
        await db.commit()
    assert first["source"] == "quant_model" and first["report"]["llm_failed"]
    assert first["usage"]["input_tokens"] == 3000 and first["usage"]["llm_failed"]   # partial usage kept
    spent = msgs.calls
    async with AsyncSessionLocal() as db:
        again = await svc.report(db, "SYN", allow_llm=True)
    assert msgs.calls == spent and again["id"] == first["id"]          # no automatic re-run

    restarted = ResearchService(market=market, snapshot_fetcher=make_snapshot,
                                analyst=ClaudeAnalyst(client=_client(msgs), web_search=False))
    async with AsyncSessionLocal() as db:
        assert await restarted.llm_backoff_active(db, "SYN")            # persisted, survives restarts
        await restarted.report(db, "SYN", allow_llm=True)
        assert msgs.calls == spent
        await restarted.report(db, "SYN", allow_llm=True, refresh=True)  # explicit refresh overrides
        await db.commit()
    assert msgs.calls > spent


async def test_concurrent_claude_runs_for_a_symbol_are_deduplicated(db_tables, monkeypatch):
    from backend.database.session import AsyncSessionLocal

    monkeypatch.setattr(settings, "RESEARCH_LLM_ENABLED", True)
    msgs = _CountingMessages(base_report(), delay=0.05)
    svc = ResearchService(market=FakeMarket({"SYN": make_trending(1)}), snapshot_fetcher=make_snapshot,
                          analyst=ClaudeAnalyst(client=_client(msgs), web_search=False))

    async def one():
        async with AsyncSessionLocal() as db:
            r = await svc.report(db, "SYN", allow_llm=True, refresh=True)
            await db.commit()
            return r

    a, b = await asyncio.gather(one(), one())
    assert msgs.calls == 1 and a["id"] == b["id"] and a["source"] == "claude"


def test_report_endpoint_runs_paid_analyst_only_on_explicit_request(db_tables, research, monkeypatch):
    from backend.main import app

    monkeypatch.setattr(settings, "RESEARCH_LLM_ENABLED", True)
    msgs = _CountingMessages(base_report())
    research.analyst = ClaudeAnalyst(client=_client(msgs), web_search=False)
    with TestClient(app) as c:
        h_admin, h = _tokens(c)
        page = c.get("/api/research/SYN/report", headers=h_admin).json()      # admin page load: free
        assert page["source"] == "quant_model" and page["ai_generation_available"] and msgs.calls == 0
        gen = c.post("/api/research/SYN/report", headers=h_admin).json()      # explicit: paid
        assert gen["source"] == "claude" and msgs.calls == 1
        assert c.get("/api/research/SYN/report", headers=h_admin).json()["id"] == gen["id"] and msgs.calls == 1
        assert c.post("/api/research/SYN/report", headers=h).json()["source"] == "quant_model"   # non-admin
        assert c.get("/api/research/SYN/report", headers=h).json()["ai_generation_available"] is False
        assert c.get("/api/research/SYN/report?refresh=true", headers=h).json()["source"] == "quant_model"
        assert msgs.calls == 1
        assert c.get("/api/research/SYN/report?refresh=true", headers=h_admin).json()["source"] == "claude"
        assert msgs.calls == 2


# 10. No research-fetch amplification from public / low-privilege endpoints.
async def test_failures_are_negatively_cached_and_cache_only_view_never_fetches():
    calls = []

    def fetch(sym):
        calls.append(sym)
        if sym.startswith("ZZ"):
            raise ValueError("no such ticker")
        if sym == "NET":
            raise ConnectionError("reset by peer")
        return make_snapshot(sym)

    svc = ResearchService(market=FakeMarket({"SYN": make_trending(1)}), snapshot_fetcher=fetch)
    for sym in ("ZZ1", "NET"):
        for _ in range(3):
            with pytest.raises(ValueError):
                await svc.snapshot(sym)
    assert calls == ["ZZ1", "NET"]
    assert (await svc.fundamental_view("ZZ1"))["available"] is False and calls == ["ZZ1", "NET"]

    assert (await svc.cached_fundamental_view("SYN"))["available"] is False and "SYN" not in calls
    await svc.snapshot("SYN")
    assert (await svc.cached_fundamental_view("SYN"))["available"] is True and calls.count("SYN") == 1


async def test_public_signal_endpoint_does_not_trigger_research_fetch(db_tables, monkeypatch):
    import backend.research.service as rs
    import backend.signals.service as ss

    calls = []
    svc = ResearchService(market=FakeMarket({"SYN": make_trending(1)}),
                          snapshot_fetcher=lambda s: calls.append(s) or make_snapshot(s))
    monkeypatch.setattr(rs, "research_service", svc)
    monkeypatch.setattr(ss, "market_data_service", svc.market)

    class Offline:
        async def predict(self, s):
            raise ValueError("offline")

        async def sentiment(self, s):
            return None

    monkeypatch.setattr(ss, "prediction_service", Offline())
    sig = await ss.signal_service.generate_signal("SYN")
    assert calls == [] and sig["components"]["fundamentals"]["available"] is False
    await svc.snapshot("SYN")                                    # e.g. loaded by the bot or a signed-in user
    sig = await ss.signal_service.generate_signal("SYN")
    assert calls == ["SYN"] and sig["components"]["fundamentals"]["available"] is True


def test_non_admin_screen_size_is_capped(db_tables, research, monkeypatch):
    from backend.main import app
    import backend.research.router as rr

    monkeypatch.setitem(rr.UNIVERSES, "big_universe", [f"S{i}" for i in range(rr.NON_ADMIN_MAX_SCREEN_SYMBOLS + 1)])
    with TestClient(app) as c:
        h_admin, h = _tokens(c)
        assert c.post("/api/research/screen", headers=h, json={"universe": "big_universe"}).status_code == 422
        assert c.post("/api/research/screen", headers=h_admin, json={"universe": "big_universe"}).status_code == 200


# 11. Usage: iterations (server-side fallback attempts), cache tokens and web searches.
def test_usage_sums_fallback_iterations_cache_tokens_and_web_searches():
    u = SimpleNamespace(
        input_tokens=120, output_tokens=50, cache_read_input_tokens=30,      # top level: final attempt only
        iterations=[{"type": "message", "input_tokens": 100, "output_tokens": 0, "cache_creation_input_tokens": 40},
                    {"type": "fallback_message", "input_tokens": 120, "output_tokens": 50, "cache_read_input_tokens": 30}],
        server_tool_use=SimpleNamespace(web_search_requests=2))
    acc = add_usage(empty_usage(), u)
    assert acc == {"input_tokens": 220, "output_tokens": 50, "cache_creation_input_tokens": 40,
                   "cache_read_input_tokens": 30, "web_search_requests": 2, "api_calls": 1, "fallback_attempts": 1}
    acc = add_usage(acc, SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=7,
                                         iterations=None, server_tool_use={"web_search_requests": 1}))
    assert acc["input_tokens"] == 230 and acc["cache_creation_input_tokens"] == 47 and acc["web_search_requests"] == 3
    assert add_usage(acc, None)["api_calls"] == 3
