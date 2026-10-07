"""The trading agent driving AlpacaBroker against the in-memory Alpaca simulator:
order-id uniqueness, lost confirmations, bracket-leg exits, broker-side splits."""
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, update

from backend.database.models import BotCalibration, BotDecision, BotOrder, BotPosition, BotTrade
from backend.database.session import AsyncSessionLocal, as_utc
from backend.trading.agent import TradingAgent
from backend.trading.broker import AlpacaBroker
from tests.conftest import FakeMarket, FakePredictor, FakeResearch, make_trending
from tests.test_agent import rows, set_state
from tests.test_alpaca import FakeAlpaca, no_sleep


@pytest.fixture
def market():
    return FakeMarket({"AAA": make_trending(1), "BBB": make_trending(2)})


def setup(market, **fake_kw):
    fake = FakeAlpaca(**fake_kw)
    for s, df in market.frames.items():
        fake.prices[s] = round(float(df["close"].iloc[-1]), 2)

    def factory(db):
        return AlpacaBroker("k", "s", live=False, transport=httpx.MockTransport(fake.handler), sleep=no_sleep,
                            fill_timeout=0.2, poll_interval=0)

    agent = TradingAgent(market=market, predictor=FakePredictor(), broker_factory=factory, use_llm=False,
                         research=FakeResearch())
    return fake, agent


async def calibrated():
    async with AsyncSessionLocal() as db:
        db.add(BotCalibration(symbol="__OOS__", stats={"trades": 80, "expectancy_r": 0.2}))
        await db.commit()


async def forget_trades():
    """Lets a test re-enter today (the same-day re-entry rule would otherwise block it)."""
    async with AsyncSessionLocal() as db:
        await db.execute(delete(BotTrade))
        await db.commit()


async def test_every_flatten_uses_a_fresh_client_order_id(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    assert (await agent.run_cycle(force=True))["entries"]
    assert (await agent.flatten_all("first", wait_seconds=1))["status"] == "done"
    assert not fake.positions

    await set_state(halted=False, halt_reason=None, flatten_requested=False)
    await forget_trades()
    assert (await agent.run_cycle(force=True))["entries"]
    second = await agent.flatten_all("second", wait_seconds=1)
    assert second["status"] == "done", second       # a reused id would be rejected with 422 forever
    assert not fake.positions
    sent = [o.get("client_order_id") for o in fake.orders.values() if o.get("client_order_id")]
    assert len(sent) == len(set(sent))


async def test_entry_whose_confirmation_was_lost_is_recovered_and_flattened(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    fake.lose_next_post_response = True                 # Alpaca fills it; we never hear back
    await agent.run_cycle(force=True)
    pending = [o for o in await rows(BotOrder) if o.status == "pending"]
    assert len(pending) == 1
    sym = pending[0].symbol
    assert sym in fake.positions and sym not in {p.symbol for p in await rows(BotPosition)}

    result = await agent.flatten_all("panic", wait_seconds=1)
    assert result["status"] == "done", result
    assert not fake.positions                           # the orphan was closed too
    assert any(d.action == "RECOVER" and d.symbol == sym for d in await rows(BotDecision))
    assert not [o for o in await rows(BotOrder) if o.status == "pending"]


async def test_recovered_entry_keeps_its_original_stop(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    fake.lose_next_post_response = True
    await agent.run_cycle(force=True)
    order = [o for o in await rows(BotOrder) if o.status == "pending"][0]
    await agent.run_cycle()                              # a paused, protective cycle resolves it
    pos = {p.symbol: p for p in await rows(BotPosition)}[order.symbol]
    assert pos.meta["recovered"] and pos.stop_price == pytest.approx(order.meta["stop"])
    assert pos.qty == pytest.approx(order.qty)


async def test_bracket_stop_fill_is_booked_at_the_leg_price(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    first = await agent.run_cycle(force=True)
    sym = first["entries"][0]["symbol"]
    pos = {p.symbol: p for p in await rows(BotPosition)}[sym]
    leg_px = round(pos.stop_price - 0.5, 2)
    fake.fill_leg(sym, "stop", leg_px)
    fake.prices[sym] = round(pos.entry_price * 1.02, 2)  # recovered by the time the bot looks again
    market.price_override[sym] = fake.prices[sym]
    await agent.run_cycle()
    trade = [t for t in await rows(BotTrade) if t.symbol == sym][0]
    assert trade.exit_reason == "broker_exit"
    assert trade.exit_price == pytest.approx(leg_px) and trade.pnl < 0   # not booked as a win at the quote


async def test_partial_take_profit_fill_is_booked(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    first = await agent.run_cycle(force=True)
    sym = first["entries"][0]["symbol"]
    pos = {p.symbol: p for p in await rows(BotPosition)}[sym]
    half = int(pos.qty // 2)
    limit_leg = next(leg for o in fake.orders.values() for leg in (o.get("legs") or [])
                     if leg["symbol"] == sym and leg["type"] == "limit")
    limit_leg.update(status="partially_filled", filled_qty=str(half), filled_avg_price=str(round(pos.target_price, 2)))
    fake.positions[sym]["qty"] -= half
    await agent.run_cycle()
    trade = [t for t in await rows(BotTrade) if t.symbol == sym][0]
    assert trade.qty == pytest.approx(half) and trade.exit_price == pytest.approx(round(pos.target_price, 2))
    left = {p.symbol: p for p in await rows(BotPosition)}[sym]
    assert left.qty == pytest.approx(pos.qty - half)


async def test_broker_side_split_rescales_instead_of_stopping_out(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    first = await agent.run_cycle(force=True)
    sym = first["entries"][0]["symbol"]
    pos = {p.symbol: p for p in await rows(BotPosition)}[sym]
    # Alpaca applies a 4:1 split to the position before the open; Yahoo's split list is still stale.
    fake.positions[sym] = {"qty": fake.positions[sym]["qty"] * 4, "avg": fake.positions[sym]["avg"] / 4}
    fake.prices[sym] = round(fake.prices[sym] / 4, 4)
    adjusted = market.frames[sym].copy()
    adjusted[["open", "high", "low", "close"]] /= 4
    market.frames[sym] = adjusted
    market.price_override[sym] = fake.prices[sym]
    summary = await agent.run_cycle()
    assert not any(e["symbol"] == sym for e in summary["exits"])
    after = {p.symbol: p for p in await rows(BotPosition)}[sym]
    assert after.qty == pytest.approx(pos.qty * 4) and after.stop_price == pytest.approx(pos.stop_price / 4)
    assert not await rows(BotTrade)


async def test_failed_broker_stop_update_is_retried(db_tables, market):
    fake, agent = setup(market)
    await calibrated()
    first = await agent.run_cycle(force=True)
    sym = first["entries"][0]["symbol"]
    # Pretend the position was opened 30 bars ago so the trailing stop has closes to trail on.
    opened = as_utc(market.frames[sym].index[-30].to_pydatetime()) - timedelta(hours=1)
    async with AsyncSessionLocal() as db:
        await db.execute(update(BotPosition).where(BotPosition.symbol == sym).values(opened_at=opened))
        await db.commit()
    fake.patch_fails = True
    await agent.run_cycle()
    pos = {p.symbol: p for p in await rows(BotPosition)}[sym]
    assert pos.meta.get("broker_stop_stale")
    fake.patch_fails = False
    await agent.run_cycle()
    pos = {p.symbol: p for p in await rows(BotPosition)}[sym]
    assert not pos.meta.get("broker_stop_stale")
    stop_leg = next(leg for o in fake.orders.values() for leg in (o.get("legs") or [])
                    if leg["symbol"] == sym and leg["type"] == "stop")
    assert float(stop_leg["stop_price"]) == pytest.approx(pos.stop_price, abs=0.01)
