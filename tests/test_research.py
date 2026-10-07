from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.research import scorecard as sc
from backend.research.analyst import ClaudeAnalyst, quant_report, validate_report
from backend.research.data import normalize_statement
from backend.research.fundamentals import analyze_fundamentals
from backend.research.service import ResearchService
from backend.research.valuation import dcf_enterprise_value, implied_growth, value_company
from tests.conftest import FakeMarket, make_ohlcv, make_trending
from tests.research_fixtures import make_snapshot, yf_frames


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
    assert rep["rating"] == "BUY" and rep["usage"] == {"input_tokens": 4000, "output_tokens": 1100}
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


def test_research_api(db_tables, research):
    from backend.main import app

    with TestClient(app) as c:
        c.post("/auth/register", json={"email": "a@example.com", "username": "alice", "password": "correct-horse-1"})
        c.post("/auth/register", json={"email": "b@example.com", "username": "bob", "password": "correct-horse-1"})
        tok = c.post("/auth/login", json={"email": "b@example.com", "password": "correct-horse-1"}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/research/SYN/fundamentals").status_code == 401
        d = c.get("/api/research/SYN/fundamentals", headers=h).json()
        assert d["scorecard"]["composite"] is not None and "statements" not in d["snapshot"]
        rep = c.get("/api/research/SYN/report", headers=h).json()
        assert rep["source"] == "quant_model" and rep["rating"] in ("STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL")
        scr = c.post("/api/research/screen", headers=h, json={"symbols": ["SYN", "BAD", "NOPE"]}).json()
        assert [r["symbol"] for r in scr["results"]][:2] == ["SYN", "BAD"] and "error" in scr["results"][2]
        assert c.get("/api/research/reports", headers=h).json()[0]["symbol"] == "SYN"
