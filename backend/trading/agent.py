"""
The trading agent: observe → analyse → decide → (review) → act → record.

Safety properties (each one exists because an audit found it missing):

* Exactly one trader. Every cycle takes the DB lease with a unique token; the
  lease is renewed — and ownership re-checked — before every broker action,
  so a cycle that lost its lease stops before it can place another order.
* Durable side effects. Each order is journaled as `pending` before it is
  sent; its outcome and the resulting position or trade are committed
  together, and every position/candidate is processed in isolation. An order
  whose outcome was lost (timeout, crash, unconfirmed cancel) stays `pending`
  and is resolved against the broker at the start of the next cycle, so a
  fill becomes a managed position instead of an orphan.
* Fail safe on bad data. A held position without a trustworthy price is a
  data fault: the high-water mark, daily baseline and circuit breakers are
  frozen, no new entries are taken, and the operator is alerted. A price more
  than 30% away from the last accepted mark is not trusted until it is
  explained by a split (re-checked against a fresh split feed, and at a broker
  that adjusts splits itself, against the position) or confirmed by a second
  observation, so neither a split nor a bad tick can move equity, trip the
  kill switch or trigger an exit.
* Live = backtested. Entries, exits and edge calibration use the technical
  score on completed daily bars, exactly as in the backtest. ML, sentiment,
  fundamentals and the earnings calendar can only block or shrink a trade.
* Panic button. /flatten sets a durable flag and halt. Every entry is fenced
  atomically against that flag right before it is sent; a cycle that is
  already running flattens before it releases the lease, otherwise whichever
  process takes the lease next does.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database.models import (
    BotCalibration, BotDecision, BotOrder, BotPosition, BotState, BotTrade, EquitySnapshot,
)
from backend.database.session import AsyncSessionLocal, as_utc, utcnow
from backend.market_data.service import market_data_service
from backend.notifier import notify
from backend.predictions.service import prediction_service
from backend.trading.broker import BrokerPosition, OrderResult, make_broker
from backend.trading.calibration import Calibrator
from backend.trading.costs import cost_model_for
from backend.trading.llm_reviewer import LLMReviewer, Verdict
from backend.trading.markets import REFERENCE_SYMBOLS, exchange_for, exchange_tz, trading_date
from backend.trading.regime import Regime
from backend.trading.risk import PortfolioSnapshot, RiskConfig, RiskManager, TradeProposal
from backend.trading.strategy import (
    StrategyConfig, analyze_latest, compute_factor_frame, exit_signal, update_trailing_stop,
)

logger = logging.getLogger(__name__)

LEASE_SECONDS = 600
DECISION_RETENTION_DAYS = 30
POOLED_KEY = "__POOLED__"
OOS_KEY = "__OOS__"
MIN_OOS_TRADES = 30
SUSPICIOUS_MOVE = 0.30        # unexplained move vs the last accepted mark → needs a second observation
SPLIT_RECHECK_MOVE = 0.10     # beyond this (or at the stop) the split feed is re-read, bypassing its cache
REQUOTE_TOLERANCE = 0.005
REVIEW_TIMEOUT_SECONDS = 90.0  # a slow LLM review must not delay the panic button or the cycle
CASHFLOW_LOOKBACK = timedelta(days=3)
STOP_EXIT_REASONS = ("stop", "stop_gap", "kill_switch", "broker_exit")


class LeaseLost(RuntimeError):
    """Another process took over the trading lease; stop before acting."""


async def get_state(db: AsyncSession) -> BotState:
    state = await db.get(BotState, 1)
    if state is None:
        state = BotState(id=1, enabled=False, halted=False, consecutive_losses=0, config_overrides={},
                         flatten_requested=False, consecutive_failures=0, consecutive_data_faults=0)
        db.add(state)
        await db.flush()
    return state


def config_error(state: BotState) -> Optional[str]:
    try:
        RiskConfig().with_overrides((state.config_overrides or {}).get("risk"))
        return None
    except (TypeError, ValueError) as e:
        return str(e)


def effective_configs(state: BotState) -> tuple[StrategyConfig, RiskConfig, List[str]]:
    overrides = state.config_overrides or {}
    try:
        risk_cfg = RiskConfig().with_overrides(overrides.get("risk"))
    except (TypeError, ValueError) as e:
        # A bad stored override must never stop the bot from enforcing stops:
        # fall back to defaults (entries are blocked until it is fixed).
        logger.error("Invalid stored risk overrides (%s); using defaults", e)
        risk_cfg = RiskConfig()
    universe = list(dict.fromkeys(overrides.get("universe") or settings.bot_universe))
    return StrategyConfig(), risk_cfg, universe


async def load_calibrator(db: AsyncSession, symbol: str = "") -> Calibrator:
    """The pooled walk-forward calibration (per-symbol fits were never validated OOS)."""
    row = await db.get(BotCalibration, POOLED_KEY)
    return Calibrator(row.stats if row else None)


async def load_oos(db: AsyncSession) -> Optional[Dict[str, Any]]:
    row = await db.get(BotCalibration, OOS_KEY)
    return row.stats if row else None


def exchange_today(symbol: str, now: datetime) -> date:
    return now.astimezone(exchange_tz(symbol)).date()


def _last_bar_day(df: pd.DataFrame, symbol: str) -> Optional[date]:
    if df.empty:
        return None
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    return idx[-1].tz_convert(exchange_tz(symbol)).date()


def completed_bars(df: pd.DataFrame, symbol: str, now: datetime) -> pd.DataFrame:
    """Drop today's still-forming daily bar so live signals match the backtest."""
    if df.empty:
        return df
    return df.iloc[:-1] if _last_bar_day(df, symbol) >= exchange_today(symbol, now) else df


def _coid(cycle_id: str, symbol: str, side: str) -> str:
    """A client_order_id unique per attempt: Alpaca rejects a reused id forever, so a
    retried exit (e.g. a second flatten of the same symbol) must never repeat one."""
    return f"bot-{cycle_id}-{uuid.uuid4().hex[:8]}-{side}-{symbol}"[:64]


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
        research=None,
        now_fn=utcnow,
    ):
        self.session_factory = session_factory
        self.market = market
        self.predictor = predictor
        self.broker_factory = broker_factory
        self.use_llm = settings.LLM_REVIEW_ENABLED if use_llm is None else use_llm
        self.reviewer = reviewer or (LLMReviewer() if self.use_llm else None)
        self.name = owner or "agent"
        self.now = now_fn
        if research is None:
            from backend.research.service import research_service
            research = research_service
        self.research = research

    # ─── Lease ────────────────────────────────────────────────────────────────

    async def _acquire_lease(self, db: AsyncSession, token: str) -> bool:
        now = self.now()
        res = await db.execute(
            update(BotState).where(BotState.id == 1)
            .where(or_(BotState.lease_until.is_(None), BotState.lease_until < now))
            .values(lease_owner=token, lease_until=now + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return res.rowcount == 1

    async def _renew_lease(self, db: AsyncSession, token: str) -> None:
        """Fence every broker action: extend the lease only if we still own it."""
        res = await db.execute(
            update(BotState).where(BotState.id == 1, BotState.lease_owner == token)
            .values(lease_until=self.now() + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if res.rowcount != 1:
            raise LeaseLost("trading lease lost")

    async def _release_lease(self, token: str) -> None:
        async with self.session_factory() as db:
            await db.execute(
                update(BotState).where(BotState.id == 1, BotState.lease_owner == token)
                .values(lease_owner=None, lease_until=None)
                .execution_options(synchronize_session=False)
            )
            await db.commit()

    # ─── Data helpers ─────────────────────────────────────────────────────────

    async def _frame(self, symbol: str, scfg: StrategyConfig) -> pd.DataFrame:
        df = await self.market.get_history_df(symbol, period="2y", interval="1d")
        df = completed_bars(df, symbol, self.now())
        return await asyncio.to_thread(compute_factor_frame, df, scfg)

    async def _optional(self, coro, timeout: float = 30.0):
        try:
            return await asyncio.wait_for(coro, timeout)
        except Exception as e:
            logger.info("Optional input unavailable: %s", e)
            return None

    async def _market_open(self, broker, symbol: str, cache: Dict[str, bool]) -> bool:
        ex = exchange_for(symbol)
        if ex in cache:
            return cache[ex]
        is_open = await broker.is_market_open(symbol)
        if is_open and getattr(broker, "needs_trading_day_check", False):
            is_open = await self._is_trading_day(symbol)
        cache[ex] = is_open
        return is_open

    async def _is_trading_day(self, symbol: str) -> bool:
        """
        Paper mode has no exchange calendar. Yahoo shows a bar for today once the
        exchange has traded, so today is a trading day if any liquid reference
        instrument (or the symbol itself) has one — one halted, delisted or
        mistyped universe symbol can't switch off stop enforcement for everything.
        """
        today = exchange_today(symbol, self.now())
        errors = []
        for ref in dict.fromkeys(REFERENCE_SYMBOLS.get(exchange_for(symbol), ()) + (symbol,)):
            try:
                df = await self.market.get_history_df(ref, period="5d", interval="1d")
                if _last_bar_day(df, ref) == today:
                    return True
            except Exception as e:
                errors.append(f"{ref}: {e}")
        if errors:
            logger.info("Trading-day check for %s: no bar today (%s)", exchange_for(symbol), "; ".join(errors))
        return False

    # ─── Public API ───────────────────────────────────────────────────────────

    async def run_cycle(self, force: bool = False) -> Dict[str, Any]:
        """
        One cycle. Disabled (paused) bots still run protective cycles: stops,
        targets, trailing stops and broker-side protection keep being enforced,
        but no positions are opened. `force` allows entries for one manual run.
        """
        cycle_id = uuid.uuid4().hex[:12]
        token = f"{self.name}-{uuid.uuid4().hex}"
        async with self.session_factory() as db:
            state = await get_state(db)
            await db.commit()
            allow_entries = bool(state.enabled or force)
            if not await self._acquire_lease(db, token):
                return {"cycle_id": cycle_id, "status": "busy", "message": "Another cycle holds the lease"}
        try:
            summary = await self._cycle(cycle_id, token, allow_entries, force)
            await self._record_outcome(summary, None)
            return summary
        except LeaseLost as e:
            await notify("lease_lost", f"Cycle {cycle_id} lost the trading lease and stopped", "critical")
            summary = {"cycle_id": cycle_id, "status": "lease_lost", "error": str(e)}
            await self._record_outcome(summary, str(e))
            return summary
        except Exception as e:
            logger.exception("Trading cycle %s failed", cycle_id)
            summary = {"cycle_id": cycle_id, "status": "error", "error": f"{type(e).__name__}: {e}"}
            await self._record_outcome(summary, summary["error"])
            return summary
        finally:
            await self._release_lease(token)

    async def _record_outcome(self, summary: dict, error: Optional[str]) -> None:
        try:
            async with self.session_factory() as db:
                state = await get_state(db)
                now = self.now()
                state.last_cycle_at = now
                state.last_cycle_summary = summary
                if error:
                    state.consecutive_failures = (state.consecutive_failures or 0) + 1
                    state.last_error = error[:2000]
                    if state.consecutive_failures >= 2:
                        await notify("cycle_failures", f"{state.consecutive_failures} consecutive failed cycles: {error[:300]}",
                                     "critical")
                else:
                    state.consecutive_failures = 0
                    state.last_success_at = now
                    state.last_error = None
                await db.commit()
        except Exception:
            logger.exception("Could not record cycle outcome")

    async def flatten_all(self, reason: str, wait_seconds: float = 60.0) -> Dict[str, Any]:
        """
        Panic button. Durably halts the bot and requests a flatten, then tries
        to perform it under the lease. A running cycle fences every entry
        against the flag atomically and flattens before it releases the lease,
        so if this returns 'queued' the flatten still happens at the end of
        that cycle (or, for closed markets, at the next open).
        """
        async with self.session_factory() as db:
            state = await get_state(db)
            state.enabled = False
            state.halted = True
            state.halted_at = self.now()
            state.halt_reason = f"FLATTEN: {reason}"
            state.flatten_requested = True
            await db.commit()
        await notify("flatten_requested", f"Flatten requested ({reason})", "critical")
        token = f"flatten-{uuid.uuid4().hex}"
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            async with self.session_factory() as db:
                if await self._acquire_lease(db, token):
                    break
            if asyncio.get_running_loop().time() >= deadline:
                return {"status": "queued",
                        "message": "A cycle holds the lease; it opens nothing new and flattens before it finishes."}
            await asyncio.sleep(1.0)
        try:
            async with self.session_factory() as db:
                broker = self.broker_factory(db)
                try:
                    # Orders whose outcome was lost may have opened positions: resolve them first.
                    await self._resolve_pending_orders(db, broker, token, "flatten")
                    results = await self._flatten(db, broker, token, "manual_flatten", {})
                    state = await get_state(db)
                    await db.refresh(state)
                    done = not state.flatten_requested
                finally:
                    if hasattr(broker, "aclose"):
                        await broker.aclose()
            if done:
                return {"status": "done", "closed": results}
            return {"status": "pending", "closed": results,
                    "message": "Some positions could not be closed yet (market closed, no trustworthy price or a "
                               "broker error); every cycle retries until they are."}
        finally:
            await self._release_lease(token)

    # ─── Cycle ────────────────────────────────────────────────────────────────

    async def _cycle(self, cycle_id: str, token: str, allow_entries: bool, force: bool = False) -> Dict[str, Any]:
        async with self.session_factory() as db:
            broker = self.broker_factory(db)
            try:
                summary = await self._cycle_inner(db, broker, cycle_id, token, allow_entries, force)
                await self._late_flatten(db, broker, token, summary)
                return summary
            finally:
                if hasattr(broker, "aclose"):
                    await broker.aclose()
                try:
                    await db.execute(delete(BotDecision).where(
                        BotDecision.created_at < self.now() - timedelta(days=DECISION_RETENTION_DAYS)))
                    await db.commit()
                except Exception:
                    await db.rollback()

    async def _cycle_inner(self, db: AsyncSession, broker, cycle_id: str, token: str, allow_entries: bool,
                           force: bool = False) -> Dict[str, Any]:
        state = await get_state(db)
        scfg, rcfg, universe = effective_configs(state)
        cfg_err = config_error(state)
        risk = RiskManager(rcfg)
        now = self.now()
        summary: Dict[str, Any] = {"cycle_id": cycle_id, "status": "ok", "mode": getattr(broker, "name", "?"),
                                   "entries": [], "exits": [], "skipped": 0, "data_faults": []}
        if cfg_err:
            summary["config_error"] = cfg_err

        # ── Orders whose outcome was lost become positions (or are closed out) first ──
        await self._resolve_pending_orders(db, broker, token, cycle_id)

        # ── Observe ──
        broker_positions = {p.symbol: p for p in await broker.get_positions()}
        tracked = {p.symbol: p for p in (await db.execute(select(BotPosition))).scalars()}
        managed_symbols = set(tracked) | (set(broker_positions) if settings.BOT_ADOPT_EXTERNAL_POSITIONS else set())
        symbols = sorted(set(universe) | set(broker_positions))
        quotes = await self.market.get_quotes(symbols) if symbols else {}
        prices = {s: float(q["current_price"]) for s, q in quotes.items() if "error" not in q and q.get("current_price")}
        for sym, bp in broker_positions.items():
            if sym not in prices and bp.market_price:
                prices[sym] = bp.market_price   # broker's own mark as fallback

        # Splits and implausible prices are dealt with before anything is valued.
        if await self._check_prices(db, broker, broker_positions, tracked, prices, cycle_id):
            broker_positions = {p.symbol: p for p in await broker.get_positions()}   # post-split quantities
        missing = sorted(s for s in broker_positions if s not in prices)
        await self._reconcile(db, broker, broker_positions, tracked, prices, cycle_id, now)
        tracked = {p.symbol: p for p in (await db.execute(select(BotPosition))).scalars()}

        # ── Equity, cash flows, breakers ──
        account = await broker.get_account(prices)
        mv = {s: p.qty * prices.get(s, p.avg_price) for s, p in broker_positions.items()}
        cash_err = None
        if not missing:
            try:
                await self._apply_cash_flows(db, broker, state, account.equity, cycle_id, now)
            except Exception as e:
                logger.warning("Cash-flow check failed: %s", e)
                cash_err = f"{type(e).__name__}: {e}"
        data_fault = bool(missing) or cash_err is not None
        if data_fault:
            state.consecutive_data_faults = (state.consecutive_data_faults or 0) + 1
            summary["status"] = "degraded"
            summary["data_faults"] = missing
            why = []
            if missing:
                why.append(f"No trustworthy price for held position(s): {', '.join(missing)}.")
            if cash_err:
                summary["cashflow_error"] = cash_err
                why.append(f"Deposits/withdrawals could not be read ({cash_err}), so equity changes can't be attributed.")
            db.add(BotDecision(cycle_id=cycle_id, action="DATA_FAULT",
                               reasons=why + ["Equity, high-water mark and circuit breakers frozen; no new entries "
                                              "this cycle."]))
            if state.consecutive_data_faults >= 2:
                await notify("data_fault", f"{' '.join(why)} ({state.consecutive_data_faults} cycles)",
                             "critical" if not getattr(broker, "supports_broker_stops", False) else "warning")
        else:
            state.consecutive_data_faults = 0
            today = trading_date(now)
            if state.day_start_date != today:
                state.day_start_date = today
                state.day_start_equity = account.equity
            state.high_water_mark = max(float(state.high_water_mark or 0), account.equity)
            hwm = float(state.high_water_mark)
            db.add(EquitySnapshot(timestamp=now, equity=account.equity, cash=account.cash, exposure=sum(mv.values()),
                                  drawdown_pct=round((1 - account.equity / hwm) * 100, 4) if hwm else 0.0))

        hwm = float(state.high_water_mark or account.equity)
        cooldown = state.cooldown_until is not None and as_utc(state.cooldown_until) > now
        snap = PortfolioSnapshot(
            equity=account.equity, cash=account.cash, positions=dict(mv), high_water_mark=hwm,
            day_start_equity=float(state.day_start_equity or account.equity),
            consecutive_losses=state.consecutive_losses or 0, cooldown_active=cooldown,
        )
        breakers = risk.circuit_breakers(snap) if not data_fault else None
        summary.update(equity=round(account.equity, 2), cash=round(account.cash, 2), positions=len(broker_positions),
                       drawdown_pct=round(snap.drawdown_pct * 100, 3), daily_pnl_pct=round(snap.daily_pnl_pct * 100, 3))
        unmanaged = sorted(set(broker_positions) - managed_symbols)
        if unmanaged:
            summary["unmanaged_positions"] = unmanaged

        if breakers and breakers.kill and not state.halted:
            state.halted, state.halted_at = True, now
            state.enabled = False   # after /reset-halt the operator restarts explicitly
            state.halt_reason = "; ".join(breakers.reasons)
            state.flatten_requested = bool(rcfg.flatten_on_kill)
            db.add(BotDecision(cycle_id=cycle_id, action="HALT", reasons=breakers.reasons))
            await notify("kill_switch", state.halt_reason, "critical", {"equity": account.equity, "hwm": hwm})
        await db.commit()

        open_cache: Dict[str, bool] = {}
        is_open = {s: await self._market_open(broker, s, open_cache) for s in symbols}
        summary["market_open"] = open_cache

        # ── Flatten (kill switch or panic button — possibly pressed while we were observing) ──
        await db.refresh(state)
        if state.flatten_requested:
            summary["_flattened"] = True
            summary["exits"].extend(await self._flatten(db, broker, token, self._flatten_reason(state), is_open, prices))
            broker_positions = {p.symbol: p for p in await broker.get_positions()}
            for s in list(mv):
                if s not in broker_positions:
                    mv.pop(s)

        if not any(is_open.values()):
            summary["status"] = "market_closed" if summary["status"] == "ok" else summary["status"]
            return summary

        # ── Manage open positions (always — even when paused, halted or degraded) ──
        frames: Dict[str, pd.DataFrame] = {}
        exited: Set[str] = set()
        for sym, bp in list(broker_positions.items()):
            if sym not in managed_symbols or sym not in prices or not is_open.get(sym):
                continue
            try:
                info = await self._manage_position(db, broker, token, sym, bp, prices[sym], scfg, frames, cycle_id)
            except LeaseLost:
                raise
            except Exception as e:
                logger.exception("Managing %s failed", sym)
                await db.rollback()
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="ERROR", reasons=[f"Position management failed: {e}"]))
                await db.commit()
                continue
            if info:
                summary["exits"].append(info)
                if info.get("status") != "rejected":
                    exited.add(sym)
                    mv.pop(sym, None)

        # ── Entries ──
        await db.refresh(state)
        blocked = self._entry_blockers(state, allow_entries, force, data_fault, breakers, now, cfg_err)
        if not blocked:
            blocked = await self._broker_calibration_blocker(db, broker)
        if blocked:
            summary["status"] = summary["status"] if summary["status"] != "ok" else blocked[0]
            summary["blocked_reasons"] = blocked[1]
            if blocked[0] not in ("paused",):
                db.add(BotDecision(cycle_id=cycle_id, action="SKIP", reasons=blocked[1]))
            await db.commit()
            return summary

        account = await broker.get_account(prices)
        snap = PortfolioSnapshot(
            equity=account.equity, cash=account.cash, positions=dict(mv), high_water_mark=hwm,
            day_start_equity=float(state.day_start_equity or account.equity),
            consecutive_losses=state.consecutive_losses or 0, cooldown_active=False,
        )
        reentry_blocked = await self._reentry_blocks(db, rcfg, now)
        tradeable = [s for s in universe if is_open.get(s) and s in prices and s not in broker_positions
                     and s not in exited and s not in reentry_blocked]
        for s in universe:
            if s in reentry_blocked and s not in broker_positions:
                db.add(BotDecision(cycle_id=cycle_id, symbol=s, action="SKIP", reasons=[reentry_blocked[s]]))
        candidates = await self._find_candidates(db, tradeable, prices, scfg, frames, cycle_id, summary, now)
        await self._execute_entries(db, broker, token, candidates, snap, risk, frames, prices, cycle_id, summary, force)
        await db.commit()
        return summary

    @staticmethod
    def _flatten_reason(state: BotState) -> str:
        return "kill_switch" if (state.halt_reason or "").startswith("KILL") else "manual_flatten"

    async def _late_flatten(self, db: AsyncSession, broker, token: str, summary: Dict[str, Any]) -> None:
        """A panic pressed while this cycle was running is executed before the lease is released."""
        flattened = summary.pop("_flattened", False)
        state = await get_state(db)
        await db.refresh(state)
        if not state.flatten_requested or (flattened and not summary.get("entries")):
            return
        summary["exits"].extend(await self._flatten(db, broker, token, self._flatten_reason(state), {}))
        summary["status"] = "halted"

    def _entry_blockers(self, state: BotState, allow_entries: bool, force: bool, data_fault: bool, breakers, now,
                        cfg_err: Optional[str] = None):
        if state.flatten_requested or state.halted:
            return ("halted", [state.halt_reason or "Bot is halted"])
        if not allow_entries or not (state.enabled or force):
            return ("paused", ["Bot is paused: protective management only"])
        if cfg_err:
            return ("entries_blocked", [f"Stored risk overrides are invalid ({cfg_err}); fix them via PUT /config"])
        if data_fault:
            return ("degraded", ["Data fault: no new entries until every held position has a trustworthy price"])
        if breakers is not None and not breakers.allow_entries:
            return ("entries_blocked", breakers.reasons)
        if state.cooldown_until is not None and as_utc(state.cooldown_until) > now:
            return ("entries_blocked", [f"Cooling down after {state.consecutive_losses} consecutive losses"])
        return None

    async def _broker_calibration_blocker(self, db: AsyncSession, broker):
        if not getattr(broker, "supports_broker_stops", False) or not settings.BOT_REQUIRE_CALIBRATION_FOR_BROKER:
            return None
        oos = await load_oos(db)
        if not oos or (oos.get("trades") or 0) < MIN_OOS_TRADES or (oos.get("expectancy_r") or 0) <= 0:
            return ("entries_blocked", [
                "Broker trading requires a stored walk-forward calibration with ≥30 out-of-sample trades and "
                "positive expectancy (run POST /api/bot/calibrate)."])
        return None

    async def _reentry_blocks(self, db: AsyncSession, rcfg: RiskConfig, now) -> Dict[str, str]:
        """
        Symbols that may not be bought now: stopped out within the cooldown, or
        exited for any reason earlier today (the next entry signal would come from
        the same completed bar that just closed the trade — the backtest can only
        re-enter a bar later).
        """
        window = max(rcfg.reentry_cooldown_hours, 0.0) + 48
        rows = (await db.execute(select(BotTrade.symbol, BotTrade.exit_time, BotTrade.exit_reason).where(
            BotTrade.exit_time >= now - timedelta(hours=window)))).all()
        out: Dict[str, str] = {}
        cooldown = timedelta(hours=rcfg.reentry_cooldown_hours)
        for sym, exit_time, reason in rows:
            t = as_utc(exit_time)
            if reason in STOP_EXIT_REASONS and rcfg.reentry_cooldown_hours > 0 and t >= now - cooldown:
                out[sym] = f"Re-entry cooldown after a stop-out ({rcfg.reentry_cooldown_hours:.0f}h)"
            elif exchange_today(sym, t) == exchange_today(sym, now):
                out.setdefault(sym, "Exited earlier today: no re-entry on the same completed bar")
        return out

    # ─── Corporate actions & reconciliation ───────────────────────────────────

    async def _fetch_splits(self, sym: str, fresh: bool) -> Optional[List[dict]]:
        get_splits = getattr(self.market, "get_splits", None)
        if get_splits is None:
            return []
        try:
            return await asyncio.wait_for(get_splits(sym, fresh=fresh), 15)
        except Exception as e:
            logger.info("Split feed unavailable for %s: %s", sym, e)
            return None

    @staticmethod
    def _rescale(meta: BotPosition, r: float) -> None:
        for f in ("entry_price", "stop_price", "initial_stop", "target_price", "highest_price", "entry_atr"):
            v = getattr(meta, f)
            if v is not None:
                setattr(meta, f, float(v) / r)
        meta.qty = float(meta.qty) * r
        m = dict(meta.meta or {})
        if m.get("last_mark"):
            m["last_mark"] = float(m["last_mark"]) / r
        m.pop("suspect_price", None)
        meta.meta = m

    async def _apply_feed_splits(self, db, broker, sym: str, meta: BotPosition, splits: List[dict], cycle_id) -> float:
        """Rescale for splits since entry that the split feed reports (simulated broker). Returns the ratio."""
        m = dict(meta.meta or {})
        applied = set(m.get("applied_splits", []))
        opened = pd.Timestamp(as_utc(meta.opened_at))
        total = 1.0
        for sp in splits:
            key = f"{sp['date']}:{sp['ratio']}"
            when = pd.Timestamp(sp["date"])
            when = when.tz_localize("UTC") if when.tzinfo is None else when
            if key in applied or when <= opened or not sp["ratio"] or sp["ratio"] <= 0:
                continue
            r = float(sp["ratio"])
            self._rescale(meta, r)
            await broker.apply_split(sym, r)
            applied.add(key)
            total *= r
            meta.meta = {**(meta.meta or {}), "applied_splits": sorted(applied)}
            db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SPLIT",
                               reasons=[f"Applied {r:g}-for-1 split dated {sp['date']}: rescaled stops and quantity"]))
            await notify("split_applied", f"{sym}: applied {r:g}:1 split", "info")
        return total

    def _apply_broker_split(self, db, sym: str, meta: BotPosition, bp: BrokerPosition, cycle_id) -> float:
        """
        A broker that adjusts splits itself (Alpaca) shows one as quantity × r and
        average price ÷ r. A partial exit changes only the quantity, so it can't
        be mistaken for a split. Returns the ratio applied (1.0 if none).
        """
        held = float(meta.qty)
        entry = float(meta.entry_price)
        if held <= 0 or entry <= 0 or not bp.avg_price:
            return 1.0
        r = bp.qty / held
        if abs(r - 1) < 0.2 or abs(bp.avg_price * r / entry - 1) > 0.05:
            return 1.0
        self._rescale(meta, r)
        meta.entry_price = bp.avg_price
        db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SPLIT",
                           reasons=[f"Broker adjusted the position for a {r:g}-for-1 split: rescaled stops and targets"]))
        return r

    async def _check_prices(self, db, broker, broker_positions: Dict[str, BrokerPosition], tracked, prices: Dict[str, float],
                            cycle_id) -> bool:
        """
        Before anything is valued: explain or quarantine implausible prices of held
        positions. A big move first re-reads the split feed without its cache (on
        a split's ex-date the cached list is stale); an unexplained move of more
        than SUSPICIOUS_MOVE is dropped from `prices` (→ data fault: equity, HWM
        and breakers frozen, no action on it) until a second observation confirms
        it. Returns True if a split changed quantities.
        """
        changed = False
        broker_adjusts = getattr(broker, "adjusts_splits", False)
        for sym, meta in tracked.items():
            bp = broker_positions.get(sym)
            if bp is None:
                continue
            price = prices.get(sym)
            m = dict(meta.meta or {})
            ref = float(m.get("last_mark") or meta.highest_price or meta.entry_price)
            jumped = price is not None and ref > 0 and (abs(price / ref - 1) > SPLIT_RECHECK_MOVE
                                                        or price <= float(meta.stop_price))
            if broker_adjusts:
                ratio = self._apply_broker_split(db, sym, meta, bp, cycle_id)
            else:
                splits = await self._fetch_splits(sym, fresh=jumped)
                ratio = await self._apply_feed_splits(db, broker, sym, meta, splits or [], cycle_id)
                changed = changed or ratio != 1.0
            if ratio != 1.0:
                m = dict(meta.meta or {})
                ref = float(m.get("last_mark") or meta.highest_price or meta.entry_price)
            if price is None:
                continue
            move = price / ref - 1 if ref > 0 else 0.0
            if abs(move) > SUSPICIOUS_MOVE:
                suspect = m.get("suspect_price")
                if suspect and abs(price / float(suspect) - 1) < 0.10:
                    m.pop("suspect_price", None)
                    m["last_mark"] = price
                    db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="DATA_FAULT",
                                       reasons=[f"Move to {price:.2f} ({move:+.0%} vs last mark {ref:.2f}) confirmed by a "
                                                "second observation and no split explains it: acting on it"]))
                else:
                    m["suspect_price"] = price
                    prices.pop(sym, None)
                    db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="DATA_FAULT",
                                       reasons=[f"Price {price:.2f} is {move:+.0%} vs last mark {ref:.2f} and no split "
                                                "explains it: not trusted until a second observation confirms it"]))
                    await notify("suspicious_move", f"{sym} quoted {move:+.0%} vs last mark; awaiting confirmation",
                                 "warning")
            else:
                m.pop("suspect_price", None)
                m["last_mark"] = price
            meta.meta = m
        await db.commit()
        return changed

    async def _apply_cash_flows(self, db, broker, state: BotState, equity: float, cycle_id, now) -> float:
        """
        Deposits and withdrawals are not performance: shift the high-water mark and
        the daily baseline by them. Activities are read over a window that overlaps
        the previous check and de-duplicated by id, so a flow posted while a cycle
        is running is counted exactly once. Raises if the broker can't answer.
        """
        checked = as_utc(state.cashflow_checked_at) if state.cashflow_checked_at else None
        since = (checked or now) - CASHFLOW_LOOKBACK
        activities = await broker.get_cash_activities(since)
        seen = dict(state.cashflow_seen or {})
        new = [a for a in activities if str(a["id"]) not in seen]
        for a in new:
            seen[str(a["id"])] = now.isoformat()
        # An id first seen before the window minus a day can no longer be returned again.
        horizon = since - timedelta(days=1)
        state.cashflow_seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= horizon}
        state.cashflow_checked_at = now
        if checked is None:
            return 0.0   # first check: earlier flows are already part of the starting equity
        flows = float(sum(float(a["amount"]) for a in new))
        if flows:
            state.high_water_mark = float(state.high_water_mark or equity) + flows
            if state.day_start_equity is not None:
                state.day_start_equity = float(state.day_start_equity) + flows
            db.add(BotDecision(cycle_id=cycle_id, action="CASHFLOW", reasons=[f"Net external cash flow {flows:+,.2f}"]))
        return flows

    # ─── Orders with an unknown outcome ───────────────────────────────────────

    async def _resolve_pending_orders(self, db, broker, token, cycle_id) -> None:
        """
        An order still 'pending' in the journal was sent but its outcome was never
        recorded (timeout, crash, cancel not confirmed). Ask the broker what
        happened: a filled entry becomes a managed position with the stop and
        target it was sent with; a filled exit is booked by reconciliation.
        """
        pending = (await db.execute(select(BotOrder).where(BotOrder.status == "pending")
                                    .order_by(BotOrder.id))).scalars().all()
        for order in pending:
            om = dict(order.meta or {})
            coid = om.get("client_order_id") or order.broker_order_id
            try:
                await self._renew_lease(db, token)
                info = await broker.lookup_order(coid) if coid else None
            except LeaseLost:
                raise
            except Exception as e:
                logger.warning("Could not resolve pending order %s: %s", coid, e)
                continue   # still unknown: try again next cycle
            if info is not None and not info.get("done"):
                continue   # cancel not confirmed yet
            filled = float((info or {}).get("filled_qty") or 0)
            if filled <= 0:
                order.status = "cancelled" if info is not None else "rejected"
                order.meta = {**om, "resolved": "no fill at the broker"}
                await db.commit()
                continue
            order.status = "filled" if info.get("status") == "filled" else "partial"
            order.qty = filled
            order.fill_price = info.get("fill_price") or order.ref_price
            order.broker_order_id = info.get("order_id") or order.broker_order_id
            order.meta = {**om, "recovered": True}
            msg = f"{order.side} {filled:g} {order.symbol} @ {order.fill_price:.2f} filled after its confirmation was lost"
            if order.side == "BUY":
                if await db.get(BotPosition, order.symbol) is None:
                    stop = float(om.get("stop") or order.fill_price * 0.95)
                    db.add(BotPosition(
                        symbol=order.symbol, qty=filled, entry_price=order.fill_price, stop_price=stop, initial_stop=stop,
                        target_price=om.get("target"), highest_price=order.fill_price, entry_atr=om.get("atr"),
                        entry_score=om.get("score"), entry_costs=order.commission or 0.0,
                        opened_at=as_utc(order.created_at) or self.now(),
                        meta={"recovered": True, "entry_order_id": order.broker_order_id, "regime": om.get("regime"),
                              "cycle_id": om.get("cycle_id"), "last_mark": order.fill_price},
                    ))
                    msg += ": now managed with its original stop and target"
            db.add(BotDecision(cycle_id=cycle_id, symbol=order.symbol, action="RECOVER", qty=filled, reasons=[msg]))
            await db.commit()
            await notify("order_recovered", msg, "warning")

    async def _reconcile(self, db, broker, broker_positions: Dict[str, BrokerPosition], tracked, prices, cycle_id, now) -> None:
        """Keep bot_positions consistent with what the broker actually holds."""
        for sym, meta in tracked.items():
            if sym not in broker_positions:
                # Closed outside the bot (e.g. an Alpaca bracket leg filled between cycles).
                fill = await self._exit_fill(db, broker, meta)
                exit_price = (fill or {}).get("price") or prices.get(sym)
                if exit_price is None:
                    db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="DATA_FAULT",
                                       reasons=["Position closed at broker but exit price unknown; recorded at stop"]))
                    exit_price = float(meta.stop_price)
                await self._record_trade(db, meta, float(exit_price), 0.0, "broker_exit", now)
                await db.delete(meta)
        for sym, bp in broker_positions.items():
            meta = tracked.get(sym)
            if meta is None:
                if not settings.BOT_ADOPT_EXTERNAL_POSITIONS:
                    continue
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
                continue
            held = float(meta.qty)
            if bp.qty < held - 1e-6 and getattr(broker, "supports_broker_stops", False):
                # Part of the position was sold at the broker (e.g. a take-profit leg partly filled).
                reduced = held - bp.qty
                fill = await self._exit_fill(db, broker, meta)
                exit_price = float((fill or {}).get("price") or prices.get(sym) or meta.stop_price)
                await self._record_trade(db, meta, exit_price, 0.0, "broker_exit", now, qty=reduced)
                meta.qty = bp.qty
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SELL", qty=reduced,
                                   reasons=[f"Broker sold {reduced:g} of {held:g} outside the bot at {exit_price:.2f}"]))
            elif abs(held - bp.qty) > 1e-6:
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SYNC", qty=bp.qty,
                                   reasons=[f"Quantity synced to the broker: {held:g} → {bp.qty:g}"]))
                meta.qty = bp.qty
            # Use the broker's actual average fill as the entry price (once).
            if getattr(broker, "supports_broker_stops", False) and not (meta.meta or {}).get("entry_synced"):
                if bp.avg_price and abs(float(meta.entry_price) / bp.avg_price - 1) > 1e-4:
                    meta.entry_price = bp.avg_price
                meta.meta = {**(meta.meta or {}), "entry_synced": True}
        await db.commit()

    async def _exit_fill(self, db, broker, meta: BotPosition) -> Optional[Dict[str, Any]]:
        """The real price a position (or part of it) was closed at outside the normal exit path."""
        rows = (await db.execute(select(BotOrder).where(
            BotOrder.symbol == meta.symbol, BotOrder.side == "SELL", BotOrder.status.in_(("filled", "partial")))
            .order_by(BotOrder.id.desc()).limit(5))).scalars().all()
        for row in rows:   # an exit whose confirmation was lost and has just been recovered
            om = row.meta or {}
            if om.get("recovered") and not om.get("booked") and row.fill_price:
                row.meta = {**om, "booked": True}
                return {"price": float(row.fill_price), "qty": float(row.qty), "via": "recovered_order"}
        try:
            return await broker.get_exit_fill(meta.symbol, (meta.meta or {}).get("entry_order_id"),
                                              as_utc(meta.opened_at))
        except Exception as e:
            logger.warning("Could not read the exit fill for %s: %s", meta.symbol, e)
            return None

    # ─── Position management ──────────────────────────────────────────────────

    async def _manage_position(self, db, broker, token, sym, bp: BrokerPosition, price, scfg, frames, cycle_id) -> Optional[dict]:
        meta = await db.get(BotPosition, sym)
        if meta is None:
            return None
        qty = bp.qty
        try:
            frame = frames.get(sym)
            if frame is None:
                frame = frames[sym] = await self._frame(sym, scfg)
        except Exception as e:
            frame = None
            logger.warning("No history to manage %s (%s); hard stop/target still enforced", sym, e)

        stop = float(meta.stop_price)
        if price <= stop:
            return await self._exit(db, broker, token, sym, qty, price, "stop", cycle_id)
        if meta.target_price and price >= float(meta.target_price):
            return await self._exit(db, broker, token, sym, qty, price, "target", cycle_id)

        if getattr(broker, "supports_broker_stops", False):
            await self._renew_lease(db, token)
            prot = await broker.ensure_protection(sym, qty, stop)
            if prot.get("action") == "placed":
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="PROTECT",
                                   reasons=[f"Re-armed missing broker stop for {prot['qty']} sh at {prot['stop']}"]))
            elif prot.get("action") == "failed":
                await notify("protection_failed", f"{sym}: could not place broker stop: {prot.get('error')}", "critical")

        if frame is None or len(frame) == 0:
            await db.commit()
            return None

        # Trailing stop on completed daily closes (same as the backtest).
        opened = pd.Timestamp(as_utc(meta.opened_at))
        since_entry = frame[frame.index > opened]
        highest = float(meta.highest_price or meta.entry_price)
        new_stop = stop
        atr = float(frame["ATR_14"].iloc[-1])
        rt_cost = cost_model_for(sym).round_trip_cost_pct(float(meta.entry_price), qty)
        for close in since_entry["close"]:
            new_stop, highest = update_trailing_stop(new_stop, float(meta.entry_price), highest, float(close), atr,
                                                     float(meta.entry_atr or atr), rt_cost, scfg)
        meta.highest_price = highest
        m = dict(meta.meta or {})
        raised = new_stop > stop + 1e-9
        if raised:
            meta.stop_price = new_stop
            db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="TRAIL", reasons=[f"Stop raised {stop:.2f} → {new_stop:.2f}"]))
        if getattr(broker, "supports_broker_stops", False) and (raised or m.get("broker_stop_stale")):
            await self._renew_lease(db, token)
            want = float(meta.stop_price)
            if await broker.update_stop(sym, want):
                m.pop("broker_stop_stale", None)
            else:
                if not m.get("broker_stop_stale"):
                    await notify("protection_failed", f"{sym}: broker stop could not be raised to {want:.2f}; the bot "
                                                      "enforces it every cycle and keeps retrying", "warning")
                m["broker_stop_stale"] = True
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="ERROR",
                                   reasons=[f"Broker stop update to {want:.2f} failed; retrying next cycle"]))
        meta.meta = m
        await db.commit()

        score = float(frame["tech_score"].iloc[-1]) if not math.isnan(frame["tech_score"].iloc[-1]) else 0.0
        reason = exit_signal(score, len(since_entry), scfg)
        if reason:
            return await self._exit(db, broker, token, sym, qty, price, reason, cycle_id)
        return None

    # ─── Candidate selection ──────────────────────────────────────────────────

    async def _find_candidates(self, db, universe, prices, scfg, frames, cycle_id, summary, now) -> List[dict]:
        candidates = []
        sem = asyncio.Semaphore(4)

        async def analyse(sym: str):
            async with sem:
                try:
                    frame = frames.get(sym)
                    if frame is None:
                        frame = frames[sym] = await self._frame(sym, scfg)
                    ml, sentiment, fundamental = await asyncio.gather(
                        self._optional(self.predictor.predict(sym)),
                        self._optional(self.predictor.sentiment(sym)),
                        self._optional(self.research.fundamental_view(sym), timeout=30),
                    )
                    return sym, analyze_latest(sym, frame, scfg, ml=ml, sentiment=sentiment, fundamental=fundamental), \
                        fundamental, None
                except Exception as e:
                    return sym, None, None, str(e)

        calibrator = await load_calibrator(db)
        results = await asyncio.gather(*(analyse(s) for s in universe))
        for sym, a, fundamental, err in results:
            if a is None:
                db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SKIP", reasons=[f"Analysis failed: {err}"]))
                summary["skipped"] += 1
                continue
            threshold = scfg.entry_threshold + (scfg.bear_entry_penalty if a.regime == Regime.BEAR_TREND.value else 0.0)
            base = dict(cycle_id=cycle_id, symbol=sym, score=a.tech_score, regime=a.regime)
            # The validated signal: technical score on completed bars.
            if a.tech_score < threshold:
                db.add(BotDecision(action="HOLD", reasons=[f"Technical score {a.tech_score:+.2f} < entry {threshold:.2f}"]
                                   + a.reasons, **base))
                continue
            vetoes = []
            if a.score < threshold:
                vetoes.append(f"Overlays (ML/sentiment/fundamentals) pull the blended score to {a.score:+.2f}")
            live = prices[sym]
            if abs(live - a.price) > scfg.gap_filter_atr * a.atr:
                vetoes.append(f"Price {live:.2f} moved more than {scfg.gap_filter_atr} ATR from the signal close {a.price:.2f}")
            if fundamental and fundamental.get("available"):
                # An uncorroborated Altman Z'' distress reading (profitable, cash-generative, interest
                # covered; book equity shrunk by buybacks) is a research flag, not a veto.
                if fundamental.get("altman_zone") == "distress" and fundamental.get("altman_distress_corroborated", True):
                    vetoes.append("Altman Z in the distress zone")
                if fundamental.get("piotroski") is not None and fundamental["piotroski"] <= 2:
                    vetoes.append(f"Piotroski F-score {fundamental['piotroski']}/9")
            # Runs whether or not fundamentals are available: it fails closed when they could not be fetched.
            vetoes.extend(self._earnings_vetoes(sym, fundamental, now))
            if vetoes:
                db.add(BotDecision(action="SKIP", reasons=vetoes + a.reasons, **base))
                summary["skipped"] += 1
                continue
            edge = calibrator.estimate(a.tech_score)
            stop = live - (a.price - a.stop)
            target = live + (a.target - a.price)
            candidates.append({"symbol": sym, "analysis": a, "edge": edge, "price": live, "stop": stop,
                               "target": target, "fundamental": fundamental})
        await db.commit()
        candidates.sort(key=lambda c: c["edge"].ev_r, reverse=True)
        return candidates

    @staticmethod
    def _earnings_vetoes(sym: str, fundamental: Optional[dict], now) -> List[str]:
        n = settings.EARNINGS_BLACKOUT_DAYS
        if n <= 0:
            return []
        # Fail CLOSED, for the earnings blackout only. If the research view could not be obtained (None:
        # the 30 s budget expired on a cold cache, or the call failed) or its fetch failed (`fetch_failed`:
        # Yahoo down or rate limited), the next report date is unknown, so no entry is opened: an earnings
        # gap can jump the stop. This does not outlast the outage: a cold-cache fetch keeps running in the
        # background and is cached, a failed fetch is negatively cached for only
        # research.service.TRANSIENT_FAILURE_TTL (2 min) and a snapshot whose calendar failed for
        # DEGRADED_SNAPSHOT_TTL (10 min, under one cycle), so the first cycle after Yahoo recovers checks
        # the real date. A view that is unavailable because Yahoo answered with no fundamentals at all
        # (no `fetch_failed`, e.g. ETF-like instruments) has no earnings date to check and does not block.
        # The Altman/Piotroski vetoes need data and are simply skipped when it is missing.
        if fundamental is None:
            return ["Earnings date unknown (fundamentals timed out or failed): blackout can't be checked"]
        if not fundamental.get("available"):
            if fundamental.get("fetch_failed") or fundamental.get("earnings_unknown"):
                why = fundamental.get("reason") or "fundamentals fetch failed"
                return [f"Earnings date unknown ({why}): blackout can't be checked"]
            return []
        if fundamental.get("earnings_unknown"):
            return ["Earnings date unknown (calendar fetch failed): blackout can't be checked"]
        start = fundamental.get("next_earnings")
        if not start:
            return []
        end = fundamental.get("next_earnings_end") or start
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            return []
        today = exchange_today(sym, now)
        # Unconfirmed dates come as a [start, end] window: blocked from N days before the start to the end.
        if first - timedelta(days=n) <= today <= last:
            window = start if end == start else f"{start} to {end}"
            return [f"Earnings {window}: gap-risk blackout ({n}d before through the report)"]
        return []

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

    # ─── Entry execution ──────────────────────────────────────────────────────

    async def _execute_entries(self, db, broker, token, candidates, snap: PortfolioSnapshot, risk: RiskManager,
                               frames, prices, cycle_id, summary, force: bool) -> None:
        for c in candidates:
            sym, a, edge = c["symbol"], c["analysis"], c["edge"]
            # Re-check the durable switches before every order (panic button, halt, losses).
            state = await get_state(db)
            await db.refresh(state)
            if state.flatten_requested or state.halted or not (state.enabled or force):
                summary["blocked_reasons"] = ["Halted/paused during the cycle"]
                break
            if state.cooldown_until is not None and as_utc(state.cooldown_until) > self.now():
                summary["blocked_reasons"] = ["Loss-streak cooldown started during the cycle"]
                break
            base = dict(cycle_id=cycle_id, symbol=sym, score=a.tech_score, regime=a.regime)
            try:
                for held in snap.positions:
                    if held not in frames:
                        try:
                            frames[held] = await self._frame(held, StrategyConfig())
                        except Exception:
                            pass
                corr = self._correlations(sym, list(snap.positions), frames)
                proposal = TradeProposal(sym, c["price"], c["stop"], c["target"], a.tech_score, a.regime, edge,
                                         cost_model_for(sym))
                decision = risk.evaluate(proposal, snap, correlations=corr)
                if not decision.approved:
                    db.add(BotDecision(action="SKIP", expected_edge_pct=decision.expected_edge_pct,
                                       cost_pct=decision.cost_pct, reasons=decision.reasons + a.reasons, **base))
                    summary["skipped"] += 1
                    await db.commit()
                    continue

                qty = decision.qty
                verdict: Optional[Verdict] = None
                if self.reviewer is not None:
                    try:
                        verdict = await asyncio.wait_for(self.reviewer.review(
                            self._review_payload(a, edge, decision, c), self._tool_handler(snap, prices)),
                            REVIEW_TIMEOUT_SECONDS)
                    except Exception as e:  # reviewer bugs or timeouts must not take the cycle down
                        verdict = self.reviewer._fail_safe(f"{type(e).__name__}") if hasattr(self.reviewer, "_fail_safe") \
                            else Verdict("veto", 0.0, f"Reviewer error: {e}", source="fail_safe")
                    qty = int(math.floor(qty * verdict.size_multiplier))
                    if qty <= 0:
                        db.add(BotDecision(action="SKIP", qty=0, reasons=[f"LLM veto: {verdict.rationale}"] + a.reasons,
                                           llm_verdict=verdict.to_dict(), **base))
                        summary["skipped"] += 1
                        await db.commit()
                        continue

                # Re-quote right before sending; re-size if the price moved.
                price, stop, target = c["price"], c["stop"], c["target"]
                fresh = await self._optional(self.market.get_quote(sym), timeout=10)
                if fresh and fresh.get("current_price"):
                    new_price = float(fresh["current_price"])
                    if abs(new_price / price - 1) > REQUOTE_TOLERANCE:
                        stop, target = new_price - (price - stop), new_price + (target - price)
                        price = new_price
                        decision = risk.evaluate(TradeProposal(sym, price, stop, target, a.tech_score, a.regime, edge,
                                                               cost_model_for(sym)),
                                                 snap, correlations=corr)
                        if not decision.approved:
                            db.add(BotDecision(action="SKIP", reasons=["Re-quote changed the trade:"] + decision.reasons, **base))
                            await db.commit()
                            continue
                        qty = min(qty, decision.qty)

                intent = {"stop": stop, "target": target, "atr": a.atr, "score": a.tech_score, "regime": a.regime,
                          "cycle_id": cycle_id}
                sent = await self._send_buy(db, broker, token, sym, qty, price, stop, target, cycle_id, intent, force)
                if sent is None:   # halted, paused or flattened while this candidate was being prepared
                    summary["blocked_reasons"] = ["Halted/paused during the cycle"]
                    db.add(BotDecision(action="SKIP", reasons=["Halted/paused before the order was sent"], **base))
                    await db.commit()
                    break
                order, result = sent
                if not result.filled:
                    db.add(BotDecision(action="SKIP", reasons=[f"Entry {result.status}: {result.message}"], **base))
                    await db.commit()   # a 'pending' outcome stays pending and is resolved next cycle
                    if result.status in ("rejected", "pending"):
                        await notify("order_rejected" if result.status == "rejected" else "order_unknown",
                                     f"{sym} entry {result.status}: {result.message}", "warning")
                    continue
                fill = result.fill_price or price
                filled_qty = result.qty
                # The position is committed together with the order's outcome.
                db.add(BotPosition(
                    symbol=sym, qty=filled_qty, entry_price=fill, stop_price=stop, initial_stop=stop,
                    target_price=target, highest_price=fill, entry_atr=a.atr, entry_score=a.tech_score,
                    entry_costs=result.commission, opened_at=self.now(),
                    meta={"regime": a.regime, "edge": edge.to_dict(), "cycle_id": cycle_id,
                          "blended_score": a.score, "fundamental": c.get("fundamental"),
                          "entry_order_id": result.broker_order_id, "last_mark": fill},
                ))
                db.add(BotDecision(action="BUY", qty=filled_qty, expected_edge_pct=decision.expected_edge_pct,
                                   cost_pct=decision.cost_pct, reasons=decision.reasons + a.reasons,
                                   llm_verdict=verdict.to_dict() if verdict else None, **base))
                await db.commit()
                # Later candidates in this cycle see this position (cash, exposure, correlation).
                snap.cash -= filled_qty * fill + result.commission
                snap.positions[sym] = filled_qty * fill
                summary["entries"].append({"symbol": sym, "qty": filled_qty, "price": round(fill, 4),
                                           "stop": round(stop, 4), "target": round(target, 4), "score": a.tech_score})
            except LeaseLost:
                raise
            except Exception as e:
                logger.exception("Entry for %s failed", sym)
                await db.rollback()
                db.add(BotDecision(action="ERROR", reasons=[f"Entry processing failed: {e}"], **base))
                await db.commit()

    async def _fence_entry(self, db, token: str, force: bool) -> bool:
        """
        Renew the lease only if we still own it AND the bot may still open trades.
        One atomic UPDATE, so a panic button pressed at any point before it wins.
        """
        conds = [BotState.id == 1, BotState.lease_owner == token,
                 BotState.flatten_requested.is_(False), BotState.halted.is_(False)]
        if not force:
            conds.append(BotState.enabled.is_(True))
        res = await db.execute(update(BotState).where(*conds)
                               .values(lease_until=self.now() + timedelta(seconds=LEASE_SECONDS))
                               .execution_options(synchronize_session=False))
        await db.commit()
        if res.rowcount == 1:
            return True
        if await db.scalar(select(BotState.lease_owner).where(BotState.id == 1)) != token:
            raise LeaseLost("trading lease lost")
        return False

    async def _send_buy(self, db, broker, token, sym, qty, price, stop, target, cycle_id, intent: dict,
                        force: bool) -> Optional[Tuple[BotOrder, OrderResult]]:
        """Send an entry; the caller commits its outcome together with the position."""
        if not await self._fence_entry(db, token, force):
            return None
        coid = _coid(cycle_id, sym, "buy")
        order = BotOrder(symbol=sym, side="BUY", qty=qty, ref_price=price, status="pending", reason="entry",
                         broker=getattr(broker, "name", "?"), broker_order_id=coid,
                         meta={"client_order_id": coid, **intent})
        db.add(order)
        await db.commit()
        result = await broker.buy(sym, qty, price, stop, target, client_order_id=coid)
        order.status = result.status
        order.fill_price = result.fill_price
        order.commission = result.commission
        if result.broker_order_id:
            order.broker_order_id = result.broker_order_id
        if result.filled:
            order.qty = result.qty
        return order, result

    # ─── Exits ────────────────────────────────────────────────────────────────

    async def _flatten(self, db, broker, token, reason: str, is_open: Dict[str, bool], prices: Optional[dict] = None) -> List[dict]:
        positions = await broker.get_positions()
        rows = {p.symbol: p for p in (await db.execute(select(BotPosition))).scalars()}
        unresolved = set((await db.execute(select(BotOrder.symbol).where(BotOrder.status == "pending"))).scalars())
        tracked = set(rows) | unresolved
        if prices is None or any(p.symbol not in prices for p in positions):
            quotes = await self.market.get_quotes([p.symbol for p in positions]) if positions else {}
            prices = {**(prices or {}), **{s: float(q["current_price"]) for s, q in quotes.items()
                                           if "error" not in q and q.get("current_price")}}
        results = []
        cache: Dict[str, bool] = {}
        for p in positions:
            if p.symbol not in tracked and not settings.BOT_ADOPT_EXTERNAL_POSITIONS:
                continue
            open_now = is_open.get(p.symbol) if p.symbol in is_open else await self._market_open(broker, p.symbol, cache)
            price = prices.get(p.symbol) or p.market_price
            if not open_now or not price:
                results.append({"symbol": p.symbol, "status": "deferred",
                                "reason": "market closed" if not open_now else "no price"})
                continue
            mark = ((rows[p.symbol].meta or {}).get("last_mark") if p.symbol in rows else None)
            if mark and not getattr(broker, "supports_broker_stops", False) and \
                    abs(price / float(mark) - 1) > SUSPICIOUS_MOVE:
                # The simulator fills at our quote: never at an unconfirmed outlier.
                results.append({"symbol": p.symbol, "status": "deferred", "reason": "price unconfirmed"})
                continue
            try:
                results.append(await self._exit(db, broker, token, p.symbol, p.qty, price, reason, "flatten"))
            except LeaseLost:
                raise
            except Exception as e:
                logger.exception("Flatten of %s failed", p.symbol)
                results.append({"symbol": p.symbol, "status": "error", "reason": str(e)})
        remaining = [p for p in await broker.get_positions()
                     if p.symbol in tracked or settings.BOT_ADOPT_EXTERNAL_POSITIONS]
        state = await get_state(db)
        if not remaining:
            state.flatten_requested = False
            await notify("flatten_done", f"All positions closed ({reason})", "critical")
        await db.commit()
        return results

    async def _exit(self, db, broker, token, sym, qty, price, reason, cycle_id) -> dict:
        await self._renew_lease(db, token)
        meta = await db.get(BotPosition, sym)
        coid = _coid(cycle_id, sym, "sell")
        order = BotOrder(symbol=sym, side="SELL", qty=qty, ref_price=price, status="pending", reason=reason,
                         broker=getattr(broker, "name", "?"), broker_order_id=coid, meta={"client_order_id": coid})
        db.add(order)
        await db.commit()
        result = await broker.sell(sym, qty, price, client_order_id=coid,
                                   protective_stop=float(meta.stop_price) if meta is not None else None)
        order.status = result.status
        order.fill_price = result.fill_price
        order.commission = result.commission
        if result.broker_order_id:
            order.broker_order_id = result.broker_order_id
        if not result.filled:
            db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="ERROR",
                               reasons=[f"Exit ({reason}) {result.status}: {result.message}",
                                        f"Protection: {result.extra.get('reprotected')}"]))
            await db.commit()
            await notify("exit_failed", f"{sym} exit ({reason}) failed: {result.message}", "critical")
            return {"symbol": sym, "status": "rejected", "reason": reason}
        fill = result.fill_price or price
        filled_qty = min(result.qty or qty, qty)
        if meta is not None:
            await self._record_trade(db, meta, fill, result.commission, reason, self.now(), qty=filled_qty)
            if filled_qty >= float(meta.qty) - 1e-9:
                await db.delete(meta)
            else:
                meta.qty = float(meta.qty) - filled_qty
        db.add(BotDecision(cycle_id=cycle_id, symbol=sym, action="SELL", qty=filled_qty,
                           reasons=[f"Exit: {reason} at {fill:.2f}" + (" (partial)" if result.status == "partial" else "")]))
        await db.commit()
        return {"symbol": sym, "qty": filled_qty, "price": round(fill, 4), "reason": reason, "status": result.status}

    async def _record_trade(self, db, meta: BotPosition, exit_price: float, exit_fees: float, reason: str, now,
                            qty: Optional[float] = None) -> None:
        qty = float(qty if qty is not None else meta.qty)
        entry = float(meta.entry_price)
        share = qty / float(meta.qty) if meta.qty else 1.0
        costs = float(meta.entry_costs or 0) * share + exit_fees
        pnl = qty * (exit_price - entry) - costs
        # Same R definition as the backtest: risk = entry fill − slipped stop fill.
        risk_ps = entry - cost_model_for(meta.symbol).fill_price("SELL", float(meta.initial_stop))
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
                await notify("loss_streak", f"{state.consecutive_losses} consecutive losses: entries paused "
                                            f"{rcfg.cooldown_hours:.0f}h", "warning")
        else:
            state.consecutive_losses = 0

    # ─── LLM reviewer plumbing ────────────────────────────────────────────────

    def _review_payload(self, a, edge, decision, c) -> dict:
        f = c.get("fundamental") or {}
        return {
            "symbol": a.symbol, "side": "BUY", "live_price": round(c["price"], 4),
            "quantity": decision.qty, "notional": decision.notional,
            "stop_loss": round(c["stop"], 4), "take_profit": round(c["target"], 4),
            "risk_pct_of_equity": decision.risk_pct_of_equity,
            "regime": a.regime, "technical_score": a.tech_score, "blended_score": a.score,
            "factor_scores": a.components, "edge_estimate": edge.to_dict(),
            "expected_gross_edge_pct": decision.expected_edge_pct, "round_trip_cost_pct": decision.cost_pct,
            "fundamentals": {k: f.get(k) for k in ("composite", "piotroski", "altman_zone", "flags", "fair_value",
                                                   "upside_pct", "next_earnings", "next_earnings_end")} if f else None,
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
