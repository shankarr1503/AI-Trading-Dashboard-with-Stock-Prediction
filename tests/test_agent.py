"""End-to-end agent cycles against the paper broker on a real (SQLite) database,
including regression tests for every confirmed finding of the readiness audit."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from backend.database.models import (
    BotCalibration, BotDecision, BotOrder, BotPosition, BotState, BotTrade, EquitySnapshot, PaperAccount, PaperPosition,
)
from backend.database.session import AsyncSessionLocal
from backend.trading.agent import TradingAgent, completed_bars, get_state
from backend.trading.broker import PaperBroker
from backend.trading.llm_reviewer import Verdict
from tests.conftest import FakeMarket, FakePredictor, FakeResearch, make_ohlcv, make_trending


class AlwaysOpenPaperBroker(PaperBroker):
    needs_trading_day_check = False

    async def is_market_open(self, symbol):
        return True


def broker_factory(db):
    return AlwaysOpenPaperBroker(db, 100_000)


@pytest.fixture
def market():
    return FakeMarket({"AAA": make_trending(1), "BBB": make_trending(2), "CCC": make_ohlcv(3, n=500)})


def agent_for(market, research=None, factory=broker_factory, **kw):
    return TradingAgent(market=market, predictor=FakePredictor(), broker_factory=factory, use_llm=False,
                        research=research or FakeResearch(), **kw)


async def rows(model):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(model))).scalars().all()


async def set_state(**kw):
    async with AsyncSessionLocal() as db:
        state = await get_state(db)
        for k, v in kw.items():
            setattr(state, k, v)
        await db.commit()


# ─── Basic behaviour ─────────────────────────────────────────────────────────

async def test_disabled_bot_runs_protective_cycle_only(db_tables, market):
    summary = await agent_for(market).run_cycle()
    assert summary["status"] == "paused"
    assert summary["entries"] == []
    assert await rows(BotOrder) == []


async def test_forced_cycle_opens_positions_within_limits(db_tables, market):
    summary = await agent_for(market).run_cycle(force=True)
    assert summary["status"] == "ok", summary
    assert summary["entries"], "uptrending symbols should produce entries"
    positions = await rows(BotPosition)
    assert {p.symbol for p in positions} == {e["symbol"] for e in summary["entries"]}
    for p in positions:
        assert p.stop_price < p.entry_price < p.target_price
        # Uncalibrated edge → half of the 1% risk budget.
        assert (p.entry_price - p.stop_price) * p.qty <= 100_000 * 0.005 * 1.05
    orders = await rows(BotOrder)
    assert orders and all(o.status == "filled" for o in orders)
    assert (await rows(PaperAccount))[0].cash < 100_000
    decided = {d.symbol for d in await rows(BotDecision) if d.symbol}
    assert {"AAA", "BBB", "CCC"} <= decided


async def test_stop_loss_exit_records_trade(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    pos = (await rows(BotPosition))[0]
    market.price_override[pos.symbol] = pos.stop_price * 0.999
    summary = await agent.run_cycle(force=True)
    assert any(e["symbol"] == pos.symbol and e["reason"] == "stop" for e in summary["exits"])
    trade = [t for t in await rows(BotTrade) if t.symbol == pos.symbol][0]
    assert trade.pnl < 0 and trade.exit_reason == "stop"
    assert trade.r_multiple == pytest.approx(-1, abs=0.3)


async def test_kill_switch_halts_and_flattens(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    held = {p.symbol for p in await rows(BotPosition)}
    assert held
    await set_state(high_water_mark=200_000)
    summary = await agent.run_cycle(force=True)
    assert summary["status"] == "halted"
    assert {e["symbol"] for e in summary["exits"]} == held
    assert await rows(BotPosition) == [] and await rows(PaperPosition) == []
    state = (await rows(BotState))[0]
    assert state.halted and state.halt_reason.startswith("KILL") and not state.flatten_requested
    again = await agent.run_cycle(force=True)
    assert again["entries"] == []


async def test_llm_veto_and_reviewer_crash_fail_safe(db_tables, market):
    class VetoAll:
        async def review(self, proposal, handler):
            return Verdict("veto", 0.0, "test veto")

    summary = await agent_for(market, reviewer=VetoAll()).run_cycle(force=True)
    assert summary["entries"] == [] and summary["status"] == "ok"
    assert any(d.llm_verdict and d.llm_verdict["decision"] == "veto" for d in await rows(BotDecision))

    class Crashes:
        async def review(self, proposal, handler):
            raise TypeError("Could not resolve authentication method")

        def _fail_safe(self, why):
            return Verdict("veto", 0.0, why, source="fail_safe")

    summary = await agent_for(market, reviewer=Crashes()).run_cycle(force=True)
    assert summary["status"] == "ok" and summary["entries"] == []


# ─── Concurrency: lease, fencing, panic button ───────────────────────────────

async def test_lease_is_not_reentrant_for_same_owner_name(db_tables, market):
    a, b = agent_for(market, owner="api"), agent_for(market, owner="api")
    async with AsyncSessionLocal() as db:
        await get_state(db)
        await db.commit()
        assert await a._acquire_lease(db, "api-token-1")
    assert (await b.run_cycle(force=True))["status"] == "busy"
    await a._release_lease("api-token-1")
    assert (await b.run_cycle(force=True))["status"] == "ok"


async def test_lost_lease_stops_further_orders(db_tables, market):
    sent = []

    class Thief(AlwaysOpenPaperBroker):
        async def buy(self, symbol, *a, **kw):
            sent.append(symbol)
            async with AsyncSessionLocal() as other:   # another process takes over the lease
                await other.execute(update(BotState).values(lease_owner="intruder"))
                await other.commit()
            return await super().buy(symbol, *a, **kw)

    summary = await agent_for(market, factory=lambda db: Thief(db, 100_000)).run_cycle(force=True)
    assert summary["status"] == "lease_lost"
    assert len(sent) == 1   # the second candidate was never sent


async def test_flatten_request_mid_cycle_blocks_remaining_entries(db_tables, market):
    sent = []

    class AdminPanics(AlwaysOpenPaperBroker):
        async def buy(self, symbol, *a, **kw):
            sent.append(symbol)
            # The admin presses the panic button while this order is in flight.
            await set_state(flatten_requested=True, halted=True, halt_reason="FLATTEN: test", enabled=False)
            return await super().buy(symbol, *a, **kw)

    agent = agent_for(market, factory=lambda db: AdminPanics(db, 100_000))
    await agent.run_cycle(force=True)
    assert len(sent) == 1
    summary = await agent_for(market).run_cycle()          # next cycle executes the flatten
    assert summary["exits"] and await rows(BotPosition) == []
    assert not (await rows(BotState))[0].flatten_requested


async def test_flatten_all_halts_and_closes(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    result = await agent.flatten_all("test", wait_seconds=1)
    assert result["status"] == "done" and result["closed"]
    state = (await rows(BotState))[0]
    assert state.halted and not state.enabled and not state.flatten_requested
    assert await rows(BotPosition) == []


# ─── Durability and isolation ────────────────────────────────────────────────

async def test_exit_survives_later_failure(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    pos = (await rows(BotPosition))[0]
    market.price_override[pos.symbol] = pos.stop_price * 0.999

    class BuyExplodes(AlwaysOpenPaperBroker):
        async def buy(self, *a, **kw):
            raise RuntimeError("broker API exploded")

    await set_state(enabled=True)
    summary = await agent_for(market, factory=lambda db: BuyExplodes(db, 100_000)).run_cycle()
    assert summary["status"] in ("ok", "entries_blocked")
    assert any(t.symbol == pos.symbol and t.exit_reason == "stop" for t in await rows(BotTrade))
    assert pos.symbol not in {p.symbol for p in await rows(BotPosition)}


async def test_missing_quote_is_a_data_fault(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    held = [p.symbol for p in await rows(BotPosition)]
    snapshots_before = len(await rows(EquitySnapshot))
    hwm_before = (await rows(BotState))[0].high_water_mark
    market.quote_failures = set(held)
    summary = await agent.run_cycle(force=True)
    assert summary["status"] == "degraded" and set(summary["data_faults"]) == set(held)
    assert summary["entries"] == [] and summary["exits"] == []
    assert len(await rows(EquitySnapshot)) == snapshots_before
    state = (await rows(BotState))[0]
    assert state.high_water_mark == hwm_before and not state.halted
    assert any(d.action == "DATA_FAULT" for d in await rows(BotDecision))


async def test_split_rescales_instead_of_stopping_out(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    pos = (await rows(BotPosition))[0]
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    market.splits[pos.symbol] = [{"date": tomorrow, "ratio": 4.0}]
    # Yahoo history is split-adjusted retroactively, and the quote is post-split.
    adjusted = market.frames[pos.symbol].copy()
    adjusted[["open", "high", "low", "close"]] /= 4
    market.frames[pos.symbol] = adjusted
    market.price_override[pos.symbol] = float(adjusted["close"].iloc[-1])
    summary = await agent.run_cycle(force=True)
    assert not any(e["symbol"] == pos.symbol for e in summary["exits"])
    after = [p for p in await rows(BotPosition) if p.symbol == pos.symbol][0]
    assert after.qty == pytest.approx(pos.qty * 4)
    assert after.stop_price == pytest.approx(pos.stop_price / 4)
    paper = [p for p in await rows(PaperPosition) if p.symbol == pos.symbol][0]
    assert paper.qty == pytest.approx(pos.qty * 4)


async def test_unexplained_crash_needs_confirmation(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    pos = (await rows(BotPosition))[0]
    market.price_override[pos.symbol] = float(market.frames[pos.symbol]["close"].iloc[-1]) * 0.55
    first = await agent.run_cycle(force=True)
    assert not any(e["symbol"] == pos.symbol for e in first["exits"])
    second = await agent.run_cycle(force=True)
    assert any(e["symbol"] == pos.symbol and e["reason"] == "stop" for e in second["exits"])


# ─── Risk rules ──────────────────────────────────────────────────────────────

async def test_no_reentry_after_stop(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    pos = (await rows(BotPosition))[0]
    market.price_override[pos.symbol] = pos.stop_price * 0.999
    await agent.run_cycle(force=True)
    market.price_override.pop(pos.symbol)
    summary = await agent.run_cycle(force=True)
    assert pos.symbol not in {e["symbol"] for e in summary["entries"]}
    assert any(d.symbol == pos.symbol and "Re-entry cooldown" in " ".join(d.reasons or []) for d in await rows(BotDecision))


async def test_intra_cycle_correlation_limits_second_clone(db_tables):
    clone = make_trending(1)
    market = FakeMarket({"AAA": clone, "AAB": clone.copy()})
    await set_state(config_overrides={"universe": ["AAA", "AAB"]})
    summary = await agent_for(market).run_cycle(force=True)
    qty = {e["symbol"]: e["qty"] for e in summary["entries"]}
    assert len(qty) == 2
    assert min(qty.values()) < max(qty.values())          # the clone was sized down...
    buys = [d for d in await rows(BotDecision) if d.action == "BUY"]
    assert any("Size halved: correlated with" in " ".join(d.reasons) for d in buys)   # ...because of the first


async def test_fundamental_vetoes_and_earnings_blackout(db_tables, market):
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    research = FakeResearch({
        "AAA": {"available": True, "altman_zone": "distress", "piotroski": 5, "composite": 55, "coverage": 1},
        "BBB": {"available": True, "altman_zone": "safe", "piotroski": 7, "composite": 60, "coverage": 1,
                "next_earnings": soon},
    })
    summary = await agent_for(market, research=research).run_cycle(force=True)
    assert not {"AAA", "BBB"} & {e["symbol"] for e in summary["entries"]}
    text = " ".join(" ".join(d.reasons or []) for d in await rows(BotDecision))
    assert "distress" in text and "blackout" in text


async def test_broker_trading_requires_validated_calibration(db_tables, market):
    class BrokerLike(AlwaysOpenPaperBroker):
        supports_broker_stops = True

    summary = await agent_for(market, factory=lambda db: BrokerLike(db, 100_000)).run_cycle(force=True)
    assert summary["status"] == "entries_blocked" and summary["entries"] == []
    async with AsyncSessionLocal() as db:
        db.add(BotCalibration(symbol="__OOS__", stats={"trades": 80, "expectancy_r": 0.2}))
        await db.commit()
    summary = await agent_for(market, factory=lambda db: BrokerLike(db, 100_000)).run_cycle(force=True)
    assert summary["entries"]


async def test_cash_withdrawal_is_not_a_drawdown(db_tables, market):
    flows = {"value": 0.0}

    class WithFlows(AlwaysOpenPaperBroker):
        async def get_net_cash_flows(self, since):
            return flows["value"]

        async def get_account(self, prices):
            acct = await super().get_account(prices)
            acct.equity += flows.get("equity_adj", 0.0)
            return acct

    agent = agent_for(market, factory=lambda db: WithFlows(db, 100_000))
    await agent.run_cycle(force=True)
    flows.update(value=-15_000.0, equity_adj=-15_000.0)    # owner withdrew 15%
    summary = await agent.run_cycle(force=True)
    state = (await rows(BotState))[0]
    assert not state.halted and summary["status"] != "halted"


def test_completed_bars_drops_todays_partial_bar():
    df = make_ohlcv(1, n=30)
    # Yahoo stamps daily bars at exchange-local midnight (here New York), stored as UTC.
    df.index = df.index.tz_localize(None).tz_localize("America/New_York").tz_convert("UTC")
    now = df.index[-1].to_pydatetime() + timedelta(hours=15)   # 10:00 New York on that trading day
    assert len(completed_bars(df, "AAA", now)) == len(df) - 1
    later = now + timedelta(days=1)
    assert len(completed_bars(df, "AAA", later)) == len(df)
