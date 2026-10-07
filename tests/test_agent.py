"""End-to-end agent cycles against the paper broker on a real (SQLite) database."""
import pytest
from sqlalchemy import select

from backend.database.models import BotDecision, BotOrder, BotPosition, BotState, BotTrade, PaperAccount
from backend.database.session import AsyncSessionLocal
from backend.trading.agent import TradingAgent, get_state
from backend.trading.broker import PaperBroker
from backend.trading.llm_reviewer import Verdict
from tests.conftest import FakeMarket, FakePredictor, make_ohlcv, make_trending


class AlwaysOpenPaperBroker(PaperBroker):
    async def is_market_open(self, symbol):
        return True


def broker_factory(db):
    return AlwaysOpenPaperBroker(db, 100_000)


@pytest.fixture
def market():
    return FakeMarket({"AAA": make_trending(1), "BBB": make_trending(2), "CCC": make_ohlcv(3, n=500)})


def agent_for(market, **kw):
    return TradingAgent(market=market, predictor=FakePredictor(), broker_factory=broker_factory, use_llm=False, **kw)


async def rows(model):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(model))).scalars().all()


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
    orders = await rows(BotOrder)
    assert {p.symbol for p in positions} == {e["symbol"] for e in summary["entries"]}
    for p in positions:
        assert p.stop_price < p.entry_price < p.target_price
        risk = (p.entry_price - p.stop_price) * p.qty
        assert risk <= 100_000 * 0.01 * 1.05
    assert all(o.status == "filled" and o.commission >= 0 for o in orders)
    acct = (await rows(PaperAccount))[0]
    assert acct.cash < 100_000
    # every universe symbol was considered and journaled
    decided = {d.symbol for d in await rows(BotDecision) if d.symbol}
    assert {"AAA", "BBB", "CCC"} <= decided


async def test_stop_loss_exit_records_trade(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    pos = (await rows(BotPosition))[0]
    market.price_override[pos.symbol] = pos.stop_price * 0.999  # just through the stop
    summary = await agent.run_cycle(force=True)
    assert any(e["symbol"] == pos.symbol and e["reason"] == "stop" for e in summary["exits"])
    trades = await rows(BotTrade)
    assert trades and trades[0].pnl < 0 and trades[0].exit_reason == "stop"
    assert trades[0].r_multiple == pytest.approx(-1, abs=0.3)


async def test_kill_switch_halts_and_flattens(db_tables, market):
    agent = agent_for(market)
    await agent.run_cycle(force=True)
    held = [p.symbol for p in await rows(BotPosition)]
    assert held
    # Simulate a crash: equity falls far below the high-water mark.
    async with AsyncSessionLocal() as db:
        state = await get_state(db)
        state.high_water_mark = 200_000
        await db.commit()
    summary = await agent.run_cycle(force=True)
    assert summary["status"] == "halted"
    assert {e["symbol"] for e in summary["exits"]} == set(held)
    assert await rows(BotPosition) == []
    state = (await rows(BotState))[0]
    assert state.halted and state.halt_reason.startswith("KILL")
    # Halted bot does not re-enter
    again = await agent.run_cycle(force=True)
    assert again["entries"] == []


async def test_llm_veto_blocks_entries(db_tables, market):
    class VetoAll:
        async def review(self, proposal, handler):
            return Verdict("veto", 0.0, "test veto")

    agent = agent_for(market, reviewer=VetoAll())
    summary = await agent.run_cycle(force=True)
    assert summary["entries"] == []
    vetoes = [d for d in await rows(BotDecision) if d.llm_verdict]
    assert vetoes and all(d.llm_verdict["decision"] == "veto" for d in vetoes)


async def test_lease_prevents_concurrent_cycles(db_tables, market):
    a = agent_for(market, owner="one")
    b = agent_for(market, owner="two")
    async with AsyncSessionLocal() as db:
        await get_state(db)
        await db.commit()
        assert await a._acquire_lease(db)
    assert (await b.run_cycle(force=True))["status"] == "busy"
    await a._release_lease()
    assert (await b.run_cycle(force=True))["status"] == "ok"
