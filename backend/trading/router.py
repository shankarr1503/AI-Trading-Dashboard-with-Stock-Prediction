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
from backend.trading.agent import POOLED_KEY, TradingAgent, effective_configs, get_state
from backend.trading.backtest import BacktestConfig, run_backtest, walk_forward
from backend.trading.calibration import Calibrator
from backend.trading.risk import RiskConfig

router = APIRouter()
logger = logging.getLogger(__name__)

_agent: Optional[TradingAgent] = None


def get_agent() -> TradingAgent:
    global _agent
    if _agent is None:
        _agent = TradingAgent(owner="api")
    return _agent


def _row(obj, fields: List[str]) -> dict:
    return {f: getattr(obj, f) for f in fields}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    state = await get_state(db)
    scfg, rcfg, universe = effective_configs(state)
    positions = (await db.execute(select(BotPosition))).scalars().all()
    last_eq = (await db.execute(select(EquitySnapshot).order_by(desc(EquitySnapshot.timestamp)).limit(1))).scalar_one_or_none()
    return {
        "enabled": state.enabled,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
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
async def stop(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    """Pause new entries. Existing positions remain protected by their stops."""
    state = await get_state(db)
    state.enabled = False
    return {"enabled": False}


@router.post("/reset-halt")
async def reset_halt(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    """Acknowledge a circuit-breaker halt. Resets the high-water mark to current equity."""
    state = await get_state(db)
    last_eq = (await db.execute(select(EquitySnapshot).order_by(desc(EquitySnapshot.timestamp)).limit(1))).scalar_one_or_none()
    state.halted, state.halt_reason, state.halted_at = False, None, None
    state.consecutive_losses, state.cooldown_until = 0, None
    if last_eq:
        state.high_water_mark = last_eq.equity
    return {"halted": False}


@router.post("/run-once")
@limiter.limit("6/minute")
async def run_once(request: Request, admin: User = Depends(get_current_superuser)):
    """Run a single full cycle now (even if the bot is paused)."""
    return await get_agent().run_cycle(force=True)


@router.post("/flatten")
async def flatten(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_superuser)):
    """Panic button: pause the bot and close every position at market."""
    state = await get_state(db)
    state.enabled = False
    await db.commit()
    return {"closed": await get_agent().flatten_all("manual_flatten"), "enabled": False}


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
async def positions(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
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
async def trades(limit: int = Query(100, le=1000), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(select(BotTrade).order_by(desc(BotTrade.exit_time)).limit(limit))).scalars().all()
    fields = ["id", "symbol", "qty", "entry_price", "exit_price", "entry_time", "exit_time", "pnl", "pnl_pct", "r_multiple", "costs", "exit_reason"]
    return [_row(r, fields) for r in rows]


@router.get("/orders")
async def orders(limit: int = Query(100, le=1000), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(select(BotOrder).order_by(desc(BotOrder.created_at)).limit(limit))).scalars().all()
    fields = ["id", "symbol", "side", "qty", "ref_price", "fill_price", "commission", "status", "reason", "broker", "created_at"]
    return [_row(r, fields) for r in rows]


@router.get("/decisions")
async def decisions(
    limit: int = Query(100, le=500), symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    q = select(BotDecision).order_by(desc(BotDecision.created_at)).limit(limit)
    if symbol:
        q = q.where(BotDecision.symbol == symbol.upper())
    rows = (await db.execute(q)).scalars().all()
    fields = ["id", "cycle_id", "symbol", "action", "score", "regime", "expected_edge_pct", "cost_pct", "qty", "reasons", "llm_verdict", "created_at"]
    return [_row(r, fields) for r in rows]


@router.get("/equity")
async def equity(limit: int = Query(2000, le=20000), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(select(EquitySnapshot).order_by(desc(EquitySnapshot.timestamp)).limit(limit))).scalars().all()
    return [_row(r, ["timestamp", "equity", "cash", "exposure", "drawdown_pct"]) for r in reversed(rows)]


@router.get("/performance")
async def performance(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
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
async def backtest(request: Request, body: BacktestRequest, user: User = Depends(get_current_user)):
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
    """Walk-forward backtest the universe and store the learned edge statistics the live bot uses."""
    state = await get_state(db)
    _, rcfg, universe = effective_configs(state)
    data = await _load_history(universe, "5y")
    cfg = BacktestConfig(risk=rcfg)
    result = await asyncio.to_thread(walk_forward, data, cfg, 4)
    pooled = result["calibration"]
    trades = (await asyncio.to_thread(run_backtest, data, cfg)).trades
    per_symbol = {s: Calibrator.fit([t for t in trades if t["symbol"] == s]).to_dict() for s in data}
    now = utcnow()
    for key, stats in {POOLED_KEY: pooled, **per_symbol}.items():
        row = await db.get(BotCalibration, key)
        if row is None:
            db.add(BotCalibration(symbol=key, stats=stats, updated_at=now))
        else:
            row.stats, row.updated_at = stats, now
    return {"out_of_sample": result["out_of_sample"], "calibration": pooled, "symbols": list(data)}
