"""
The trading agent: observe → analyse → decide → (review) → act → record.

One `run_cycle()` call:
  1. Takes a database lease so only one process trades at a time.
  2. Reads the broker account and positions; reconciles them with the bot's
     own stop/target records.
  3. Updates the high-water mark and daily P&L; trips circuit breakers
     (daily loss limit, consecutive-loss cooldown, drawdown kill switch).
  4. Manages every open position: hard stops, targets, trailing stops,
     signal-reversal and time exits.
  5. If entries are allowed: analyses the universe (technicals + regime +
     ML + sentiment), estimates edge, asks the risk manager to size each
     candidate, optionally asks the LLM reviewer, then places orders.
  6. Journals every decision — including the trades it chose NOT to make and why.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database.models import (
    BotCalibration, BotDecision, BotOrder, BotPosition, BotState, BotTrade, EquitySnapshot,
)
from backend.database.session import AsyncSessionLocal, as_utc, utcnow
from backend.market_data.service import market_data_service
from backend.predictions.service import prediction_service
from backend.trading.broker import make_broker
from backend.trading.calibration import Calibrator
from backend.trading.costs import cost_model_for
from backend.trading.llm_reviewer import LLMReviewer, Verdict
from backend.trading.markets import exchange_for, trading_date
from backend.trading.risk import PortfolioSnapshot, RiskConfig, RiskManager, TradeProposal
from backend.trading.strategy import (
    StrategyConfig, analyze_latest, compute_factor_frame, exit_signal, update_trailing_stop,
)

logger = logging.getLogger(__name__)

LEASE_SECONDS = 600
DECISION_RETENTION_DAYS = 30
POOLED_KEY = "__POOLED__"
MIN_SYMBOL_TRADES = 30


async def get_state(db: AsyncSession) -> BotState:
    state = await db.get(BotState, 1)
    if state is None:
        state = BotState(id=1, enabled=False, halted=False, consecutive_losses=0, config_overrides={})
        db.add(state)
        await db.flush()
    return state


def effective_configs(state: BotState) -> tuple[StrategyConfig, RiskConfig, List[str]]:
    overrides = state.config_overrides or {}
    risk_cfg = RiskConfig().with_overrides(overrides.get("risk"))
    universe = overrides.get("universe") or settings.bot_universe
    return StrategyConfig(), risk_cfg, list(universe)


async def load_calibrator(db: AsyncSession, symbol: str) -> Calibrator:
    rows = {r.symbol: r.stats for r in (await db.execute(
        select(BotCalibration).where(BotCalibration.symbol.in_([symbol, POOLED_KEY]))
    )).scalars()}
    own = rows.get(symbol)
    if own and sum(b.get("n", 0) for b in own.values()) >= MIN_SYMBOL_TRADES:
        return Calibrator(own)
    return Calibrator(rows.get(POOLED_KEY))


class TradingAgent:
    def __init__(
        self,
        session_factory=AsyncSessionLocal,
        market=market_data_service,
        predictor=prediction_service,
        broker_factory=make_broker,
        reviewer: Optional[LLMReviewer] = None,
        use_llm: Optional[bool] = None,
        owner: Optional[str] = None,
    ):
        self.session_factory = session_factory
        self.market = market
        self.predictor = predictor
        self.broker_factory = broker_factory
        self.use_llm = settings.LLM_REVIEW_ENABLED if use_llm is None else use_llm
        self.reviewer = reviewer or (LLMReviewer() if self.use_llm else None)
        self.owner = owner or f"agent-{uuid.uuid4().hex[:8]}"

    # ─── Lease (single active trader) ─────────────────────────────────────────

    async def _acquire_lease(self, db: AsyncSession) -> bool:
        now = utcnow()
        res = await db.execute(
            update(BotState)
            .where(BotState.id == 1)
            .where(or_(BotState.lease_until.is_(None), BotState.lease_until < now, BotState.lease_owner == self.owner))
            .values(lease_owner=self.owner, lease_until=now + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return res.rowcount == 1

    async def _release_lease(self) -> None:
        async with self.session_factory() as db:
            await db.execute(
                update(BotState).where(BotState.id == 1, BotState.lease_owner == self.owner)
                .values(lease_owner=None, lease_until=None)
                .execution_options(synchronize_session=False)
            )
            await db.commit()

    # ─── Data helpers ─────────────────────────────────────────────────────────

    async def _frame(self, symbol: str, scfg: StrategyConfig) -> pd.DataFrame:
        df = await self.market.get_history_df(symbol, period="2y", interval="1d")
        return await asyncio.to_thread(compute_factor_frame, df, scfg)

    async def _optional(self, coro, timeout: float = 30.0):
        try:
            return await asyncio.wait_for(coro, timeout)
        except Exception as e:
            logger.info("Optional input unavailable: %s", e)
            return None

    # ─── Public API ───────────────────────────────────────────────────────────

    async def run_cycle(self, force: bool = False) -> Dict[str, Any]:
        """
        Run one cycle. When the bot is disabled (paused), the cycle still runs in
        protective mode — stops, targets and trailing stops on existing positions
        keep being enforced — but no new positions are opened. `force` runs a
        full cycle even when disabled (manual "run once").
        """
        cycle_id = uuid.uuid4().hex[:12]
        async with self.session_factory() as db:
            state = await get_state(db)
            await db.commit()
            allow_entries = bool(state.enabled or force)
            if not await self._acquire_lease(db):
                return {"cycle_id": cycle_id, "status": "busy", "message": "Another cycle holds the lease"}
        try:
            async with self.session_factory() as db:
                summary = await self._cycle(db, cycle_id, allow_entries)
                await db.commit()
                return summary
        except Exception:
            logger.exception("Trading cycle %s failed", cycle_id)
            raise
        finally:
            await self._release_lease()

    async def flatten_all(self, reason: str) -> List[dict]:
        """Close every position immediately (kill switch / manual panic button)."""
        async with self.session_factory() as db:
            broker = self.broker_factory(db)
            try:
                positions = await broker.get_positions()
                quotes = await self.market.get_quotes([p.symbol for p in positions]) if positions else {}
                results = []
                for p in positions:
                    price = (quotes.get(p.symbol) or {}).get("current_price") or p.avg_price
                    results.append(await self._exit(db, broker, p.symbol, p.qty, price, reason, "manual"))
                await db.commit()
                return results
            finally:
                if hasattr(broker, "aclose"):
                    await broker.aclose()

    # ─── Cycle internals ──────────────────────────────────────────────────────

    async def _cycle(self, db: AsyncSession, cycle_id: str, allow_entries: bool = True) -> Dict[str, Any]:
        state = await get_state(db)
        scfg, rcfg, universe = effective_configs(state)
        risk = RiskManager(rcfg)
        broker = self.broker_factory(db)
        now = utcnow()
        summary: Dict[str, Any] = {"cycle_id": cycle_id, "status": "ok", "mode": getattr(broker, "name", "?"),
                                   "entries": [], "exits": [], "skipped": 0}
        try:
            broker_positions = {p.symbol: p for p in await broker.get_positions()}
            symbols = sorted(set(universe) | set(broker_positions))
            quotes = await self.market.get_quotes(symbols) if symbols else {}
            prices = {s: float(q["current_price"]) for s, q in quotes.items() if "error" not in q}
            account = await broker.get_account(prices)

            await self._reconcile(db, broker_positions, prices, cycle_id, now)

            # ── Equity bookkeeping & circuit breakers ──
            today = trading_date(now)
            if state.day_start_date != today:
                state.day_start_date = today
                state.day_start_equity = account.equity
            state.high_water_mark = max(float(state.high_water_mark or 0), account.equity)
            mv = {s: p.qty * prices.get(s, p.avg_price) for s, p in broker_positions.items()}
            hwm = float(state.high_water_mark)
            db.add(EquitySnapshot(timestamp=now, equity=account.equity, cash=account.cash, exposure=sum(mv.values()),
                                  drawdown_pct=round((1 - account.equity / hwm) * 100, 4) if hwm else 0.0))
            cooldown = state.cooldown_until is not None and as_utc(state.cooldown_until) > now
            snap = PortfolioSnapshot(
                equity=account.equity, cash=account.cash, positions=mv, high_water_mark=hwm,
                day_start_equity=float(state.day_start_equity or account.equity),
                consecutive_losses=state.consecutive_losses or 0, cooldown_active=cooldown,
            )
            breakers = risk.circuit_breakers(snap)
            summary.update(equity=round(account.equity, 2), cash=round(account.cash, 2), positions=len(broker_positions),
                           drawdown_pct=round(snap.drawdown_pct * 100, 3), daily_pnl_pct=round(snap.daily_pnl_pct * 100, 3))

            if breakers.kill and not state.halted:
                state.halted, state.halted_at = True, now
                state.halt_reason = "; ".join(breakers.reasons)
                db.add(BotDecision(cycle_id=cycle_id, action="HALT", reasons=breakers.reasons))
                logger.error("Kill switch tripped: %s", state.halt_reason)

            # Which exchanges are open right now (one check per exchange).
            open_by_exchange: Dict[str, bool] = {}
            for sym in symbols:
                ex = exchange_for(sym)
                if ex not in open_by_exchange:
                    open_by_exchange[ex] = await broker.is_market_open(sym)
            is_open = {s: open_by_exchange[exchange_for(s)] for s in symbols}
            summary["market_open"] = open_by_exchange

            # ── Kill switch: flatten (in open markets; the rest on the next open cycle) ──
            if state.halted and rcfg.flatten_on_kill and (state.halt_reason or "").startswith("KILL"):
                for sym, p in list(broker_positions.items()):
                    if is_open.get(sym):
                        summary["exits"].append(await self._exit(db, broker, sym, p.qty, prices.get(sym, p.avg_price), "kill_switch", cycle_id))
                        broker_positions.pop(sym, None)
                        mv.pop(sym, None)

            if not any(is_open.values()):
                summary["status"] = "market_closed"
                return self._finish(state, summary, now)

            # ── Manage open positions (always, even when paused or halted) ──
            frames: Dict[str, pd.DataFrame] = {}
            for sym, bp in list(broker_positions.items()):
                if sym not in prices or not is_open.get(sym):
                    continue
                exit_info = await self._manage_position(db, broker, sym, bp.qty, prices[sym], scfg, frames, cycle_id)
                if exit_info:
                    summary["exits"].append(exit_info)
                    if exit_info.get("status") != "rejected":
                        broker_positions.pop(sym, None)
                        mv.pop(sym, None)

            # ── Entries ──
            if not allow_entries:
                summary["status"] = "paused"
                return self._finish(state, summary, now)
            if state.halted:
                summary["status"] = "halted"
                summary["halt_reason"] = state.halt_reason
                return self._finish(state, summary, now)
            if not breakers.allow_entries:
                summary["status"] = "entries_blocked"
                summary["blocked_reasons"] = breakers.reasons
                db.add(BotDecision(cycle_id=cycle_id, action="SKIP", reasons=breakers.reasons))
                return self._finish(state, summary, now)

            account = await broker.get_account(prices)
            snap = PortfolioSnapshot(
                equity=account.equity, cash=account.cash, positions=mv, high_water_mark=hwm,
                day_start_equity=float(state.day_start_equity or account.equity),
                consecutive_losses=state.consecutive_losses or 0, cooldown_active=cooldown,
            )
            tradeable = [s for s in universe if is_open.get(s)]
            candidates = await self._find_candidates(db, tradeable, broker_positions, prices, scfg, frames, cycle_id, summary)
            await self._execute_entries(db, broker, candidates, snap, risk, frames, prices, cycle_id, summary)
            return self._finish(state, summary, now)
        finally:
            if hasattr(broker, "aclose"):
                await broker.aclose()
            await db.execute(delete(BotDecision).where(BotDecision.created_at < now - timedelta(days=DECISION_RETENTION_DAYS)))

    def _finish(self, state: BotState, summary: dict, now) -> dict:
        state.last_cycle_at = now
        state.last_cycle_summary = summary
        return summary

    async def _reconcile(self, db: AsyncSession, broker_positions: dict, prices: dict, cycle_id: str, now) -> None:
        """Keep bot_positions consistent with what the broker actually holds."""
        tracked = {p.symbol: p for p in (await db.execute(select(BotPosition))).scalars()}
        for sym, meta in tracked.items():
            if sym not in broker_positions:
                # Closed outside the bot (e.g. Alpaca bracket stop/target filled between cycles).
                exit_price = prices.get(sym, float(meta.stop_price))
                await self._record_trade(db, meta, exit_price, 0.0, "broker_exit", now)
                await db.delete(meta)
        for sym, bp in broker_positions.items():
            if sym not in tracked:
                # Position the bot didn't open: adopt it with a protective ATR stop.
                try:
                    frame = await self._frame(sym, StrategyConfig())
                    atr = float(frame["ATR_14"].iloc[-1])
                except Exception:
                    atr = bp.avg_price * 0.03
                stop = min(bp.avg_price, prices.get(sym, bp.avg_price)) - 2 * atr
                db.add(BotPosition(symbol=sym, qty=bp.qty, entry_price=bp.avg_price, stop_price=stop,
                                   initial_stop=stop, target_price=None, highest_price=bp.avg_price,
                                   entry_atr=atr, entry_score=None, opened_at=now, meta={"adopted": True}))
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="ADOPT",
                                   reasons=[f"Adopted external position with stop {stop:.2f}"]))
            elif abs(float(tracked[sym].qty) - bp.qty) > 1e-6:
                tracked[sym].qty = bp.qty
        await db.flush()

    async def _manage_position(self, db, broker, sym, qty, price, scfg, frames, cycle_id) -> Optional[dict]:
        meta = await db.get(BotPosition, sym)
        if meta is None:
            return None
        stop = float(meta.stop_price)
        if price <= stop:
            return await self._exit(db, broker, sym, qty, price, "stop", cycle_id)
        if meta.target_price and price >= float(meta.target_price):
            return await self._exit(db, broker, sym, qty, price, "target", cycle_id)

        try:
            frame = frames.get(sym)
            if frame is None:
                frame = frames[sym] = await self._frame(sym, scfg)
        except Exception as e:
            logger.warning("No data to manage %s (%s); hard stop still enforced", sym, e)
            return None

        atr = float(frame["ATR_14"].iloc[-1])
        rt_cost = cost_model_for(sym).round_trip_cost_pct(float(meta.entry_price), qty)
        new_stop, highest = update_trailing_stop(
            stop, float(meta.entry_price), float(meta.highest_price or meta.entry_price), price, atr,
            float(meta.entry_atr or atr), rt_cost, scfg,
        )
        if new_stop > stop + 1e-9:
            meta.stop_price = new_stop
            await broker.update_stop(sym, new_stop)
            db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="TRAIL",
                               reasons=[f"Stop raised {stop:.2f} → {new_stop:.2f}"]))
        meta.highest_price = highest

        opened = pd.Timestamp(as_utc(meta.opened_at))
        bars_held = int((frame.index > opened).sum())
        ml = await self._optional(self.predictor.predict(sym))
        sentiment = await self._optional(self.predictor.sentiment(sym))
        try:
            analysis = analyze_latest(sym, frame, scfg, ml=ml, sentiment=sentiment)
            reason = exit_signal(analysis.score, bars_held, scfg)
        except ValueError:
            reason = None
        if reason:
            return await self._exit(db, broker, sym, qty, price, reason, cycle_id)
        return None

    async def _find_candidates(self, db, universe, held, prices, scfg, frames, cycle_id, summary) -> List[dict]:
        candidates = []
        sem = asyncio.Semaphore(4)

        async def analyse(sym: str):
            async with sem:
                try:
                    frame = frames.get(sym)
                    if frame is None:
                        frame = frames[sym] = await self._frame(sym, scfg)
                    ml, sentiment = await asyncio.gather(
                        self._optional(self.predictor.predict(sym)),
                        self._optional(self.predictor.sentiment(sym)),
                    )
                    return sym, analyze_latest(sym, frame, scfg, ml=ml, sentiment=sentiment), None
                except Exception as e:
                    return sym, None, str(e)

        results = await asyncio.gather(*(analyse(s) for s in universe if s not in held and s in prices))
        for sym, analysis, err in results:
            if analysis is None:
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SKIP", reasons=[f"Analysis failed: {err}"]))
                summary["skipped"] += 1
                continue
            if analysis.signal != "BUY":
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="HOLD", score=analysis.score,
                                   regime=analysis.regime, reasons=analysis.reasons))
                continue
            calibrator = await load_calibrator(db, sym)
            edge = calibrator.estimate(analysis.score)
            # Re-anchor the analysis to the live price (the daily bar may be from yesterday's close).
            live = prices[sym]
            stop = live - (analysis.price - analysis.stop)
            target = live + (analysis.target - analysis.price)
            candidates.append({"symbol": sym, "analysis": analysis, "edge": edge, "price": live, "stop": stop, "target": target})
        candidates.sort(key=lambda c: c["edge"].ev_r, reverse=True)
        return candidates

    def _correlations(self, sym: str, held: List[str], frames: Dict[str, pd.DataFrame], lookback: int = 60) -> Dict[str, float]:
        out = {}
        if sym not in frames:
            return out
        r = frames[sym]["close"].pct_change().iloc[-lookback:]
        for h in held:
            if h in frames:
                rh = frames[h]["close"].pct_change().iloc[-lookback:]
                joined = pd.concat([r, rh], axis=1, join="inner").dropna()
                if len(joined) >= 20:
                    out[h] = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
        return out

    async def _execute_entries(self, db, broker, candidates, snap, risk, frames, prices, cycle_id, summary):
        reserved = 0.0
        new_positions = 0
        for c in candidates:
            sym, a, edge = c["symbol"], c["analysis"], c["edge"]
            for held in snap.positions:
                if held not in frames:
                    try:
                        frames[held] = await self._frame(held, StrategyConfig())
                    except Exception:
                        pass
            corr = self._correlations(sym, list(snap.positions), frames)
            proposal = TradeProposal(sym, c["price"], c["stop"], c["target"], a.score, a.regime, edge, cost_model_for(sym))
            decision = risk.evaluate(proposal, snap, correlations=corr, reserved_cash=reserved, pending_positions=new_positions)
            base = dict(cycle_id=cycle_id, symbol=sym, score=a.score, regime=a.regime,
                        expected_edge_pct=decision.expected_edge_pct, cost_pct=decision.cost_pct)
            if not decision.approved:
                db.add(BotDecision(action="SKIP", reasons=a.reasons + decision.reasons, **base))
                summary["skipped"] += 1
                continue

            qty = decision.qty
            verdict: Optional[Verdict] = None
            if self.reviewer is not None:
                verdict = await self.reviewer.review(
                    self._review_payload(a, edge, decision, c), self._tool_handler(snap, prices)
                )
                qty = int(math.floor(qty * verdict.size_multiplier))
                if qty <= 0:
                    db.add(BotDecision(action="SKIP", qty=0, reasons=a.reasons + [f"LLM veto: {verdict.rationale}"],
                                       llm_verdict=verdict.to_dict(), **base))
                    summary["skipped"] += 1
                    continue

            result = await broker.buy(sym, qty, c["price"], c["stop"], c["target"])
            db.add(BotOrder(symbol=sym, side="BUY", qty=qty, ref_price=c["price"], fill_price=result.fill_price,
                            commission=result.commission, status=result.status, reason="entry",
                            broker=getattr(broker, "name", "?"), broker_order_id=result.broker_order_id))
            if result.status == "rejected":
                db.add(BotDecision(action="SKIP", reasons=[f"Order rejected: {result.message}"], **base))
                continue
            fill = result.fill_price or c["price"]
            db.add(BotPosition(
                symbol=sym, qty=qty, entry_price=fill, stop_price=c["stop"], initial_stop=c["stop"],
                target_price=c["target"], highest_price=fill, entry_atr=a.atr, entry_score=a.score,
                entry_costs=result.commission, opened_at=utcnow(),
                meta={"regime": a.regime, "edge": edge.to_dict(), "cycle_id": cycle_id},
            ))
            db.add(BotDecision(action="BUY", qty=qty, reasons=a.reasons + decision.reasons,
                               llm_verdict=verdict.to_dict() if verdict else None, **base))
            reserved += qty * fill + result.commission
            new_positions += 1
            summary["entries"].append({"symbol": sym, "qty": qty, "price": round(fill, 4), "stop": round(c["stop"], 4),
                                       "target": round(c["target"], 4), "score": a.score})
        await db.flush()

    def _review_payload(self, a, edge, decision, c) -> dict:
        return {
            "symbol": a.symbol, "side": "BUY", "live_price": round(c["price"], 4),
            "quantity": decision.qty, "notional": decision.notional,
            "stop_loss": round(c["stop"], 4), "take_profit": round(c["target"], 4),
            "risk_pct_of_equity": decision.risk_pct_of_equity,
            "regime": a.regime, "composite_score": a.score, "factor_scores": a.components,
            "edge_estimate": edge.to_dict(),
            "expected_edge_pct": decision.expected_edge_pct, "round_trip_cost_pct": decision.cost_pct,
            "expected_holding_period_days": "5-40",
        }

    def _tool_handler(self, snap: PortfolioSnapshot, prices: Dict[str, float]):
        async def handler(name: str, args: dict):
            if name == "get_price_history":
                sym = str(args["symbol"]).upper()
                days = max(5, min(120, int(args.get("days", 30))))
                df = await self.market.get_history_df(sym, period="1y", interval="1d")
                tail = df.iloc[-days:]
                rets = tail["close"].pct_change().dropna()
                gaps = (tail["open"] / tail["close"].shift(1) - 1).dropna()
                return {
                    "symbol": sym,
                    "closes": [round(float(v), 2) for v in tail["close"]],
                    "first_date": str(tail.index[0].date()), "last_date": str(tail.index[-1].date()),
                    "return_pct": round(float(tail["close"].iloc[-1] / tail["close"].iloc[0] - 1) * 100, 2),
                    "daily_vol_pct": round(float(rets.std()) * 100, 3),
                    "largest_gap_pct": round(float(gaps.abs().max()) * 100, 2) if len(gaps) else 0.0,
                }
            if name == "get_news_headlines":
                return {"symbol": args["symbol"], "headlines": (await self.market.get_news(str(args["symbol"]).upper()))[:15]}
            if name == "get_portfolio":
                return {
                    "equity": round(snap.equity, 2), "cash": round(snap.cash, 2),
                    "positions": {s: round(v, 2) for s, v in snap.positions.items()},
                    "gross_exposure_pct": round(snap.gross_exposure / snap.equity * 100, 2) if snap.equity else 0,
                }
            raise ValueError(f"Unknown tool {name}")
        return handler

    async def _exit(self, db, broker, sym, qty, price, reason, cycle_id) -> dict:
        result = await broker.sell(sym, qty, price)
        db.add(BotOrder(symbol=sym, side="SELL", qty=qty, ref_price=price, fill_price=result.fill_price,
                        commission=result.commission, status=result.status, reason=reason,
                        broker=getattr(broker, "name", "?"), broker_order_id=result.broker_order_id))
        if result.status == "rejected":
            db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SKIP", reasons=[f"Exit rejected: {result.message}"]))
            return {"symbol": sym, "status": "rejected", "reason": reason}
        meta = await db.get(BotPosition, sym)
        fill = result.fill_price or price
        if meta is not None:
            await self._record_trade(db, meta, fill, result.commission, reason, utcnow())
            await db.delete(meta)
        db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SELL", qty=qty, reasons=[f"Exit: {reason} at {fill:.2f}"]))
        await db.flush()
        return {"symbol": sym, "qty": qty, "price": round(fill, 4), "reason": reason}

    async def _record_trade(self, db, meta: BotPosition, exit_price: float, exit_fees: float, reason: str, now) -> None:
        qty = float(meta.qty)
        entry = float(meta.entry_price)
        costs = float(meta.entry_costs or 0) + exit_fees
        pnl = qty * (exit_price - entry) - costs
        risk_ps = entry - float(meta.initial_stop)
        db.add(BotTrade(
            symbol=meta.symbol, qty=qty, entry_price=entry, exit_price=exit_price, entry_time=meta.opened_at,
            exit_time=now, pnl=round(pnl, 2), pnl_pct=round(pnl / (qty * entry) * 100, 3) if qty and entry else None,
            r_multiple=round(pnl / (qty * risk_ps), 3) if qty and risk_ps > 0 else None,
            costs=round(costs, 2), entry_score=meta.entry_score, exit_reason=reason,
        ))
        state = await get_state(db)
        if pnl <= 0:
            state.consecutive_losses = (state.consecutive_losses or 0) + 1
            _, rcfg, _ = effective_configs(state)
            if state.consecutive_losses >= rcfg.max_consecutive_losses:
                state.cooldown_until = now + timedelta(hours=rcfg.cooldown_hours)
        else:
            state.consecutive_losses = 0
