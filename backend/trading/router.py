"""Trading bot API: status, controls (admin), journal, analysis and backtesting."""
import asyncio
import logging
from typing import Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_current_superuser, get_current_user
from backend.config import settings
from backend.database.models import (
    BotCalibration, BotDecision, BotOrder, BotPosition, BotTrade, EquitySnapshot, User,
)
from backend.database.session import get_db, utcnow
from backend.market_data.service import market_data_service, validate_symbol
from backend.ratelimit import limiter
from backend.trading.agent import (
    OOS_KEY, POOLED_KEY, TradingAgent, config_error, effective_configs, get_state, load_oos,
)
from backend.trading.backtest import BacktestConfig, run_backtest, walk_forward
from backend.trading.risk import RiskConfig

router = APIRouter()
logger = logging.getLogger(__name__)

# The bot's book (positions, stops, orders, P&L) is private to administrators.
admin_only = get_current_superuser

# CPU-heavy simulations run one at a time so they can't starve the API.
_SIM_LOCK = asyncio.Semaphore(1)


def get_agent() -> TradingAgent:
    # A fresh agent per request: each cycle takes the lease with its own token.
    return TradingAgent(owner="api")


def _row(obj, fields: List[str]) -> dict:
    return {f: getattr(obj, f) for f in fields}


def _stale(state, now) -> bool:
    if state.last_cycle_at is None:
        return True
    from backend.database.session import as_utc

    return (now - as_utc(state.last_cycle_at)).total_seconds() > 3 * max(1, settings.BOT_CYCLE_MINUTES) * 60


@router.get("/health")
@limiter.exempt
async def bot_health(request: Request, db: AsyncSession = Depends(get_db)):
    """Liveness of the trading loop for monitors: 503 when cycles are stale or keep failing."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    state = await get_state(db)
    healthy = not _stale(state, utcnow()) and (state.consecutive_failures or 0) < 3
    body = {"healthy": healthy, "last_cycle_at": state.last_cycle_at, "last_success_at": state.last_success_at,
            "consecutive_failures": state.consecutive_failures, "halted": state.halted}
    return JSONResponse(jsonable_encoder(body), status_code=200 if healthy else 503)


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), user: User = Depends(admin_only)):
    state = await get_state(db)
    scfg, rcfg, universe = effective_configs(state)
    positions = (await db.execute(select(BotPosition))).scalars().all()
    last_eq = (await db.execute(select(EquitySnapshot).order_by(desc(EquitySnapshot.timestamp)).limit(1))).scalar_one_or_none()
    now = utcnow()
    return {
        "enabled": state.enabled,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "flatten_requested": state.flatten_requested,
        "stale": _stale(state, now),
        "consecutive_failures": state.consecutive_failures,
        "consecutive_data_faults": state.consecutive_data_faults,
        "last_error": state.last_error,
        "config_error": config_error(state),
        "last_success_at": state.last_success_at,
        "calibration": await load_oos(db),
        "mode": settings.TRADING_MODE,
        "live_trading": settings.TRADING_MODE == "alpaca_live",
        "llm_review": settings.LLM_REVIEW_ENABLED,
        "universe": universe,
        "cycle_minutes": settings.BOT_CYCLE_MINUTES,
        "last_cycle_at": state.last_cycle_at,
        "last_cycle_summary": state.last_cycle_summary,
        "high_water_mark": state.high_water_mark,
        "consecutive_losses": state.consecutive_losses,
        "cooldown_until": state.cooldown_until,
        "equity": last_eq.equity if last_eq else None,
        "cash": last_eq.cash if last_eq else None,
        "drawdown_pct": last_eq.drawdown_pct if last_eq else None,
        "open_positions": len(positions),
        "risk_config": rcfg.to_dict(),
        "strategy_config": scfg.to_dict(),
    }


@router.post("/start")
async def start(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    state = await get_state(db)
    if state.halted:
        raise HTTPException(status_code=409, detail=f"Bot is halted ({state.halt_reason}). Review, then POST /reset-halt.")
    state.enabled = True
    return {"enabled": True}


@router.post("/stop")
@limiter.exempt
async def stop(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    """Pause new entries. Existing positions remain protected by their stops."""
    state = await get_state(db)
    state.enabled = False
    return {"enabled": False}


@router.post("/reset-halt")
async def reset_halt(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    """
    Acknowledge a circuit-breaker halt. Resets the high-water mark to current
    equity and leaves the bot paused: trading resumes only with an explicit /start.
    """
    state = await get_state(db)
    if not state.halted:
        raise HTTPException(status_code=409, detail="Bot is not halted")
    if state.flatten_requested and (await db.execute(select(BotPosition))).scalars().first() is not None:
        raise HTTPException(status_code=409, detail="A flatten is still pending: positions remain open")
    state.flatten_requested = False
    last_eq = (await db.execute(select(EquitySnapshot).order_by(desc(EquitySnapshot.timestamp)).limit(1))).scalar_one_or_none()
    state.halted, state.halt_reason, state.halted_at = False, None, None
    state.enabled = False
    state.consecutive_losses, state.cooldown_until = 0, None
    if last_eq:
        state.high_water_mark = last_eq.equity
    return {"halted": False}


@router.post("/run-once")
@limiter.limit("6/minute")
async def run_once(request: Request, admin: User = Depends(get_current_superuser)):
    """Run a single full cycle now (even if the bot is paused). Returns 'busy' if a cycle is running."""
    return await get_agent().run_cycle(force=True)


@router.post("/flatten")
@limiter.exempt
async def flatten(admin: User = Depends(get_current_superuser)):
    """
    Panic button: halt the bot durably and close every position at market.
    If a cycle is running, it stops opening trades immediately and the flatten
    is executed as soon as the lease is free.
    """
    return await get_agent().flatten_all(f"manual by {admin.username}")


class ConfigUpdate(BaseModel):
    risk: Optional[Dict[str, float]] = None
    universe: Optional[List[str]] = Field(default=None, max_length=50)


@router.put("/config")
async def update_config(body: ConfigUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    state = await get_state(db)
    overrides = dict(state.config_overrides or {})
    if body.risk is not None:
        merged = {**overrides.get("risk", {}), **body.risk}
        try:
            RiskConfig().with_overrides(merged)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        overrides["risk"] = merged
    if body.universe is not None:
        try:
            overrides["universe"] = sorted({validate_symbol(s) for s in body.universe})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    state.config_overrides = overrides
    _, rcfg, universe = effective_configs(state)
    return {"risk_config": rcfg.to_dict(), "universe": universe}


@router.get("/positions")
async def positions(db: AsyncSession = Depends(get_db), user: User = Depends(admin_only)):
    rows = (await db.execute(select(BotPosition))).scalars().all()
    quotes = await market_data_service.get_quotes([r.symbol for r in rows]) if rows else {}
    out = []
    for r in rows:
        price = (quotes.get(r.symbol) or {}).get("current_price")
        pnl = (price - r.entry_price) * r.qty if price else None
        out.append({
            **_row(r, ["symbol", "qty", "entry_price", "stop_price", "initial_stop", "target_price", "highest_price", "entry_score", "opened_at"]),
            "current_price": price,
            "unrealized_pnl": round(pnl, 2) if pnl is not None else None,
            "unrealized_pnl_pct": round((price / r.entry_price - 1) * 100, 3) if price else None,
        })
    return out


@router.get("/trades")
async def trades(limit: int = Query(100, le=1000), db: AsyncSession = Depends(get_db), user: User = Depends(admin_only)):
    rows = (await db.execute(select(BotTrade).order_by(desc(BotTrade.exit_time)).limit(limit))).scalars().all()
    fields = ["id", "symbol", "qty", "entry_price", "exit_price", "entry_time", "exit_time", "pnl", "pnl_pct", "r_multiple", "costs", "exit_reason"]
    return [_row(r, fields) for r in rows]


@router.get("/orders")
async def orders(limit: int = Query(100, le=1000), db: AsyncSession = Depends(get_db), user: User = Depends(admin_only)):
    rows = (await db.execute(select(BotOrder).order_by(desc(BotOrder.created_at)).limit(limit))).scalars().all()
    fields = ["id", "symbol", "side", "qty", "ref_price", "fill_price", "commission", "status", "reason", "broker", "created_at"]
    return [_row(r, fields) for r in rows]


@router.get("/decisions")
async def decisions(
    limit: int = Query(100, le=500), symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(admin_only),
):
    q = select(BotDecision).order_by(desc(BotDecision.created_at)).limit(limit)
    if symbol:
        q = q.where(BotDecision.symbol == symbol.upper())
    rows = (await db.execute(q)).scalars().all()
    fields = ["id", "cycle_id", "symbol", "action", "score", "regime", "expected_edge_pct", "cost_pct", "qty", "reasons", "llm_verdict", "created_at"]
    return [_row(r, fields) for r in rows]


@router.get("/equity")
async def equity(limit: int = Query(2000, le=20000), db: AsyncSession = Depends(get_db), user: User = Depends(admin_only)):
    rows = (await db.execute(select(EquitySnapshot).order_by(desc(EquitySnapshot.timestamp)).limit(limit))).scalars().all()
    return [_row(r, ["timestamp", "equity", "cash", "exposure", "drawdown_pct"]) for r in reversed(rows)]


@router.get("/performance")
async def performance(db: AsyncSession = Depends(get_db), user: User = Depends(admin_only)):
    rows = (await db.execute(select(BotTrade))).scalars().all()
    if not rows:
        return {"trades": 0}
    pnls = [r.pnl for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [r.r_multiple for r in rows if r.r_multiple is not None]
    return {
        "trades": len(rows),
        "net_pnl": round(sum(pnls), 2),
        "total_costs": round(sum(r.costs or 0 for r in rows), 2),
        "win_rate_pct": round(100 * len(wins) / len(rows), 2),
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
    }


# ─── Analysis & backtesting ─────────────────────────────────────────────────

async def _load_history(symbols: List[str], period: str) -> Dict[str, pd.DataFrame]:
    async def one(s):
        try:
            return s, await market_data_service.get_history_df(s, period=period, interval="1d")
        except ValueError:
            return s, None
    pairs = await asyncio.gather(*(one(s) for s in symbols))
    data = {s: df for s, df in pairs if df is not None and not df.empty}
    if not data:
        raise HTTPException(status_code=404, detail="No historical data available for the requested symbols")
    return data


@router.get("/analyze/{symbol}")
async def analyze(symbol: str, user: User = Depends(get_current_user)):
    """What would the bot do with this symbol right now? (No order is placed.)"""
    from backend.signals.service import signal_service

    try:
        return await signal_service.generate_signal(symbol)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class BacktestRequest(BaseModel):
    symbols: List[str] = Field(min_length=1, max_length=20)
    period: str = Field("5y", pattern="^(2y|5y|10y|max)$")
    initial_capital: float = Field(100_000, gt=1000, le=1e9)
    walk_forward: bool = True
    folds: int = Field(4, ge=2, le=8)
    risk: Optional[Dict[str, float]] = None


@router.post("/backtest")
@limiter.limit("5/minute")
async def backtest(request: Request, body: BacktestRequest, user: User = Depends(admin_only)):
    """
    Backtest the bot's exact strategy, risk and cost logic on daily history.
    With walk_forward=true, edge calibration is fitted only on data preceding
    each test window and the reported metrics are out-of-sample.
    """
    try:
        symbols = [validate_symbol(s) for s in body.symbols]
        cfg = BacktestConfig(initial_capital=body.initial_capital, risk=RiskConfig().with_overrides(body.risk))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if _SIM_LOCK.locked():
        raise HTTPException(status_code=429, detail="Another backtest is running; try again shortly")
    async with _SIM_LOCK:
        data = await _load_history(symbols, body.period)
        try:
            if body.walk_forward:
                result = await asyncio.to_thread(walk_forward, data, cfg, body.folds)
            else:
                result = (await asyncio.to_thread(run_backtest, data, cfg)).to_dict()
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    curve = result.get("equity_curve", [])
    step = max(1, len(curve) // 500)
    result["equity_curve"] = curve[::step]
    result["symbols"] = list(data)
    result["disclaimer"] = "Past (and simulated) performance does not guarantee future results."
    return result


@router.post("/calibrate")
@limiter.limit("2/minute")
async def calibrate(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    """
    Walk-forward validate the universe and store (a) the pooled edge calibration
    the live bot uses and (b) the out-of-sample record that gates broker trading.
    """
    state = await get_state(db)
    _, rcfg, universe = effective_configs(state)
    if _SIM_LOCK.locked():
        raise HTTPException(status_code=429, detail="Another simulation is running; try again shortly")
    async with _SIM_LOCK:
        data = await _load_history(universe, "5y")
        cfg = BacktestConfig(risk=rcfg)
        try:
            result = await asyncio.to_thread(walk_forward, data, cfg, 4)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    oos = result["out_of_sample"]
    oos_record = {
        "trades": oos.get("trades", 0), "expectancy_r": oos.get("expectancy_r"), "sharpe": oos.get("sharpe"),
        "max_drawdown_pct": oos.get("max_drawdown_pct"), "total_return_pct": oos.get("total_return_pct"),
        "symbols": list(data), "calibration_trades": result.get("calibration_trades"), "as_of": utcnow().isoformat(),
    }
    now = utcnow()
    for key, stats in {POOLED_KEY: result["calibration"], OOS_KEY: oos_record}.items():
        row = await db.get(BotCalibration, key)
        if row is None:
            db.add(BotCalibration(symbol=key, stats=stats, updated_at=now))
        else:
            row.stats, row.updated_at = stats, now
    return {"out_of_sample": oos, "calibration": result["calibration"], "symbols": list(data),
            "broker_trading_allowed": (oos_record["trades"] or 0) >= 30 and (oos_record["expectancy_r"] or 0) > 0}
