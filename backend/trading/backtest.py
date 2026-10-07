"""
Event-driven daily-bar backtester and walk-forward validator.

Uses exactly the same strategy, calibrator, risk manager and cost model as the
live bot. Execution assumptions are deliberately conservative:

  * Signals are computed on bar t's close and executed at bar t+1's open.
  * Every fill pays spread + slippage + fees from the cost model.
  * A gap through the stop fills at the (worse) open price.
  * If a bar touches both stop and target, the stop is assumed to hit first.
  * The kill switch flattens the book and pauses trading for `kill_pause_bars`.

Walk-forward validation fits the calibrator only on data *before* each test
window, so reported out-of-sample results never see the future.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backend.trading.calibration import Calibrator
from backend.trading.costs import CostModel, cost_model_for
from backend.trading.risk import PortfolioSnapshot, RiskConfig, RiskManager, TradeProposal
from backend.trading.strategy import (
    REGIME_WEIGHTS, StrategyConfig, compute_factor_frame, exit_signal, stop_and_target, update_trailing_stop,
)
from backend.trading.regime import Regime


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    kill_pause_bars: int = 20
    correlation_lookback: int = 60
    cost_model: Optional[CostModel] = None   # None → per-symbol default


@dataclass
class _Position:
    symbol: str
    qty: int
    entry_price: float
    entry_date: pd.Timestamp
    entry_bar: int
    stop: float
    initial_stop: float
    target: float
    highest: float
    entry_atr: float
    entry_score: float
    entry_fees: float
    entry_slippage: float
    cost_model: CostModel


@dataclass
class BacktestResult:
    metrics: Dict[str, float]
    equity_curve: List[Dict]
    trades: List[Dict]
    kill_events: List[str]
    benchmark: Dict[str, float]

    def to_dict(self, include_curve: bool = True) -> dict:
        d = asdict(self)
        if not include_curve:
            d.pop("equity_curve")
        return d


def performance_metrics(equity: pd.Series, trades: List[Dict], exposure: pd.Series) -> Dict[str, float]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    rets = equity.pct_change().dropna()
    # Annualise by calendar time, not bar count: a mixed US + NSE universe has
    # more bars per year than either exchange alone.
    if isinstance(equity.index, pd.DatetimeIndex) and len(equity) > 1:
        years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    else:
        years = max(len(equity) / 252, 1e-9)
    periods_per_year = len(rets) / years if years > 0 else 252
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1.0
    vol = rets.std() * math.sqrt(periods_per_year)
    # Sortino uses downside deviation (RMS of negative returns, zeros included).
    downside = math.sqrt(float((np.minimum(rets, 0) ** 2).mean())) * math.sqrt(periods_per_year) if len(rets) else 0.0
    ann_mean = rets.mean() * periods_per_year
    sharpe = ann_mean / vol if vol > 0 else 0.0
    sortino = ann_mean / downside if downside > 0 else 0.0
    dd = equity / equity.cummax() - 1
    max_dd = float(dd.min())
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    r_mults = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    out = {
        "total_return_pct": round(total * 100, 3),
        "cagr_pct": round(cagr * 100, 3),
        "volatility_pct": round(vol * 100, 3),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown_pct": round(max_dd * 100, 3),
        "calmar": round(cagr / abs(max_dd), 3) if max_dd < 0 else 0.0,
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 2) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "expectancy_r": round(float(np.mean(r_mults)), 4) if r_mults else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "total_costs": round(sum(t["costs"] for t in trades), 2),
        "avg_exposure_pct": round(float(exposure.mean()) * 100, 2) if len(exposure) else 0.0,
        "final_equity": round(float(equity.iloc[-1]), 2),
    }
    return {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in out.items()}


def _prepare(data: Dict[str, pd.DataFrame], cfg: BacktestConfig) -> Dict[str, pd.DataFrame]:
    frames = {}
    for sym, df in data.items():
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].astype(float).dropna()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if len(df) < cfg.strategy.min_history + 5:
            continue
        frames[sym] = compute_factor_frame(df, cfg.strategy)
    return frames


def run_backtest(
    data: Dict[str, pd.DataFrame],
    cfg: Optional[BacktestConfig] = None,
    calibrator: Optional[Calibrator] = None,
    start: Optional[pd.Timestamp] = None,
    end: Optional[pd.Timestamp] = None,
    frames: Optional[Dict[str, pd.DataFrame]] = None,
    explore: bool = False,
) -> BacktestResult:
    """
    Simulate the bot on daily OHLCV data (dict symbol → DataFrame indexed by date).
    Trading happens only within [start, end]; earlier bars are used for indicator warm-up,
    and the indicator warm-up period itself is excluded from the reported results.
    `explore=True` takes every signal (no edge gates) to generate calibration data.
    """
    cfg = cfg or BacktestConfig()
    calibrator = calibrator or Calibrator()
    risk = RiskManager(cfg.risk)
    scfg = cfg.strategy
    frames = frames if frames is not None else _prepare(data, cfg)
    if not frames:
        raise ValueError("Not enough history for any symbol (need ~210+ daily bars)")

    dates = sorted(set().union(*(f.index for f in frames.values())))
    first_tradeable = min(f.index[scfg.min_history] for f in frames.values() if len(f) > scfg.min_history)
    dates = [d for d in dates if d >= first_tradeable
             and (start is None or d >= start) and (end is None or d <= end)]
    if len(dates) < 2:
        raise ValueError("Backtest window too short")
    pos_in = {s: {d: i for i, d in enumerate(f.index)} for s, f in frames.items()}
    closes = pd.DataFrame({s: f["close"] for s, f in frames.items()})
    returns = closes.pct_change()

    cash = cfg.initial_capital
    positions: Dict[str, _Position] = {}
    pending_entries: Dict[str, dict] = {}
    pending_exits: Dict[str, str] = {}
    trades: List[Dict] = []
    equity_rows: List[Dict] = []
    kill_events: List[str] = []
    hwm = cfg.initial_capital
    prev_equity = cfg.initial_capital
    consecutive_losses = 0
    cooldown_until_bar = -1
    paused_until_bar = -1
    last_close: Dict[str, float] = {}
    # Same rule as the live agent: no re-entry within reentry_cooldown_hours of a
    # stop-out. Live checks the signal of the stop day on the next day, i.e. one
    # bar later already covers 24h; each further day blocks one more signal bar.
    last_stop_bar: Dict[str, int] = {}
    reentry_block_bars = max(0, math.ceil(cfg.risk.reentry_cooldown_hours / 24) - 1)

    def cm_for(sym: str) -> CostModel:
        return cfg.cost_model or cost_model_for(sym)

    def close_position(p: _Position, raw_price: float, date, reason: str, bar_idx: int):
        nonlocal cash, consecutive_losses, cooldown_until_bar
        fill = p.cost_model.fill_price("SELL", raw_price)
        fees = p.cost_model.fees("SELL", p.qty, fill)
        cash += p.qty * fill - fees
        # entry_price is already the slipped fill, so P&L subtracts fees only;
        # `costs` reports everything paid (fees + spread/slippage on both legs).
        costs = p.entry_fees + p.entry_slippage + fees + p.qty * (raw_price - fill)
        pnl = p.qty * (fill - p.entry_price) - fees - p.entry_fees
        risk_per_share = p.entry_price - p.cost_model.fill_price("SELL", p.initial_stop)
        r_mult = pnl / (p.qty * risk_per_share) if risk_per_share > 0 else None
        trades.append({
            "symbol": p.symbol, "qty": p.qty,
            "entry_date": str(p.entry_date.date()), "exit_date": str(pd.Timestamp(date).date()),
            "entry_price": round(p.entry_price, 4), "exit_price": round(fill, 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / (p.qty * p.entry_price) * 100, 3),
            "r_multiple": round(r_mult, 3) if r_mult is not None else None,
            "costs": round(costs, 2), "entry_score": p.entry_score,
            "bars_held": bar_idx - p.entry_bar, "exit_reason": reason,
        })
        if reason in ("stop", "stop_gap", "kill_switch"):
            last_stop_bar[p.symbol] = global_bar
        consecutive_losses = consecutive_losses + 1 if pnl <= 0 else 0
        if consecutive_losses >= cfg.risk.max_consecutive_losses:
            cooldown_until_bar = global_bar + max(1, int(round(cfg.risk.cooldown_hours / 24)))
        del positions[p.symbol]

    for global_bar, date in enumerate(dates):
        # ── 1. Market-on-open exits scheduled at the previous close ──
        for sym, reason in list(pending_exits.items()):
            i = pos_in[sym].get(date)
            if i is None or sym not in positions:
                continue
            close_position(positions[sym], float(frames[sym]["open"].iloc[i]), date, reason, i)
            pending_exits.pop(sym, None)

        # ── 2. Market-on-open entries ──
        for sym, order in list(pending_entries.items()):
            i = pos_in[sym].get(date)
            if i is None:
                continue
            pending_entries.pop(sym)
            bar = frames[sym].iloc[i]
            cm = cm_for(sym)
            o = float(bar["open"])
            # Same rule as the live agent: a gap larger than gap_filter_atr × ATR
            # from the signal price is new information, so the entry is skipped;
            # otherwise stop/target are re-anchored to the actual entry price.
            if abs(o - order["signal_price"]) > scfg.gap_filter_atr * order["atr"]:
                continue
            order["stop"] = o - (order["signal_price"] - order["stop"])
            order["target"] = o + (order["target"] - order["signal_price"])
            fill = cm.fill_price("BUY", o)
            qty = order["qty"]
            fees = cm.fees("BUY", qty, fill)
            if qty * fill + fees > cash:
                qty = int((cash - cm.min_commission) / (fill * (1 + cm.buy_fee_pct + cm.commission_pct)))
                fees = cm.fees("BUY", qty, fill)
            if qty <= 0:
                continue
            cash -= qty * fill + fees
            positions[sym] = _Position(
                symbol=sym, qty=qty, entry_price=fill, entry_date=pd.Timestamp(date), entry_bar=i,
                stop=order["stop"], initial_stop=order["stop"], target=order["target"],
                highest=float(bar["open"]), entry_atr=order["atr"], entry_score=order["score"],
                entry_fees=fees, entry_slippage=qty * (fill - float(bar["open"])), cost_model=cm,
            )

        # ── 3. Intrabar stop / target ──
        for sym, p in list(positions.items()):
            i = pos_in[sym].get(date)
            if i is None:
                continue
            bar = frames[sym].iloc[i]
            o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
            if o <= p.stop:
                close_position(p, o, date, "stop_gap", i)
            elif p.target and o >= p.target:
                close_position(p, o, date, "target_gap", i)   # gapped through the target: fill at the better open
            elif l <= p.stop:
                close_position(p, p.stop, date, "stop", i)
            elif p.target and h >= p.target:
                close_position(p, p.target, date, "target", i)

        # ── 4. Mark to market ──
        for sym in frames:
            i = pos_in[sym].get(date)
            if i is not None:
                last_close[sym] = float(frames[sym]["close"].iloc[i])
        mv = {s: p.qty * last_close.get(s, p.entry_price) for s, p in positions.items()}
        equity = cash + sum(mv.values())
        hwm = max(hwm, equity)
        day_start = prev_equity
        prev_equity = equity
        equity_rows.append({
            "date": str(pd.Timestamp(date).date()), "equity": round(equity, 2), "cash": round(cash, 2),
            "exposure": round(sum(mv.values()) / equity, 4) if equity > 0 else 0.0,
            "drawdown_pct": round((1 - equity / hwm) * 100, 3),
        })

        # ── 5. Kill switch ──
        if global_bar >= paused_until_bar and equity <= hwm * (1 - cfg.risk.max_drawdown_pct):
            kill_events.append(f"{pd.Timestamp(date).date()}: drawdown {(1 - equity / hwm):.1%} → flatten & pause")
            for sym in positions:
                pending_exits[sym] = "kill_switch"
            pending_entries.clear()
            paused_until_bar = global_bar + cfg.kill_pause_bars
            continue
        if global_bar < paused_until_bar:
            if global_bar == paused_until_bar - 1:
                hwm = equity  # human "review & restart": new high-water mark
            continue

        # ── 6. Manage open positions at the close (trailing stops, exit signals) ──
        for sym, p in positions.items():
            i = pos_in[sym].get(date)
            if i is None:
                continue
            row = frames[sym].iloc[i]
            rt_cost = p.cost_model.round_trip_cost_pct(p.entry_price, p.qty)
            p.stop, p.highest = update_trailing_stop(
                p.stop, p.entry_price, p.highest, float(row["close"]), float(row["ATR_14"]),
                p.entry_atr, rt_cost, scfg,
            )
            score = float(row["tech_score"]) if not math.isnan(row["tech_score"]) else 0.0
            reason = exit_signal(score, i - p.entry_bar, scfg)
            if reason:
                pending_exits[sym] = reason

        # ── 7. New entries (decided at close, executed next open) ──
        snap = PortfolioSnapshot(
            equity=equity, cash=cash, positions=mv, high_water_mark=hwm,
            day_start_equity=day_start, consecutive_losses=consecutive_losses,
            cooldown_active=global_bar < cooldown_until_bar,
        )
        if not risk.circuit_breakers(snap).allow_entries:
            continue
        candidates = []
        for sym, f in frames.items():
            if sym in positions or sym in pending_entries:
                continue
            if sym in last_stop_bar and global_bar - last_stop_bar[sym] < reentry_block_bars:
                continue
            i = pos_in[sym].get(date)
            if i is None or i < scfg.min_history:
                continue
            row = f.iloc[i]
            score = row["tech_score"]
            atr = row["ATR_14"]
            if math.isnan(score) or not atr > 0:
                continue
            threshold = scfg.entry_threshold + (scfg.bear_entry_penalty if row["regime"] == Regime.BEAR_TREND.value else 0)
            if score < threshold:
                continue
            price = float(row["close"])
            stop, target = stop_and_target(price, float(atr), row["regime"], scfg)
            edge = calibrator.estimate(float(score))
            candidates.append((edge.ev_r, sym, price, stop, target, float(atr), float(score), row["regime"], edge))

        reserved = 0.0
        for ev_r, sym, price, stop, target, atr, score, regime, edge in sorted(candidates, reverse=True):
            corr = {}
            if positions or pending_entries:
                window = returns.loc[:date].iloc[-cfg.correlation_lookback:]
                # Entries approved earlier this bar count too (as in the live agent).
                for held in list(positions) + list(pending_entries):
                    if sym in window and held in window:
                        corr[held] = float(window[sym].corr(window[held]))
            decision = risk.evaluate(
                TradeProposal(sym, price, stop, target, score, regime, edge, cm_for(sym)),
                snap, correlations=corr, reserved_cash=reserved, pending_positions=len(pending_entries),
                explore=explore,
            )
            if decision.approved:
                pending_entries[sym] = {"qty": decision.qty, "stop": stop, "target": target, "atr": atr, "score": score,
                                        "signal_price": price}
                reserved += decision.notional * 1.01

    # Close anything still open at the last available price (marks the final result honestly).
    final_date = dates[-1]
    for sym, p in list(positions.items()):
        close_position(p, last_close.get(sym, p.entry_price), final_date, "end_of_test", pos_in[sym].get(final_date, p.entry_bar))
    if equity_rows:
        equity_rows[-1]["equity"] = round(cash, 2)

    eq = pd.Series([r["equity"] for r in equity_rows], index=pd.to_datetime([r["date"] for r in equity_rows]))
    exposure = pd.Series([r["exposure"] for r in equity_rows])
    metrics = performance_metrics(eq, trades, exposure)

    # Benchmark: equal-weight, daily-rebalanced basket of the same universe over the
    # same window (a symbol that starts trading later joins without a jump).
    window_rets = returns.loc[(returns.index >= dates[0]) & (returns.index <= dates[-1])]
    bench = (1 + window_rets.mean(axis=1).fillna(0)).cumprod() * cfg.initial_capital
    bench_metrics = performance_metrics(bench, [], pd.Series([1.0] * len(bench)))
    benchmark = {k: bench_metrics.get(k) for k in ("total_return_pct", "cagr_pct", "sharpe", "max_drawdown_pct")}
    return BacktestResult(metrics=metrics, equity_curve=equity_rows, trades=trades, kill_events=kill_events, benchmark=benchmark)


def walk_forward(
    data: Dict[str, pd.DataFrame],
    cfg: Optional[BacktestConfig] = None,
    n_folds: int = 4,
    min_train_bars: int = 252,
) -> Dict:
    """
    Anchored walk-forward validation of exactly the scheme the live bot uses:

      for each fold: calibrate on an *exploration* backtest (every signal taken)
      of all data before the test window → trade the test window with the
      normal risk gates and that calibrator.

    The returned `calibration` is fitted the same way on the full history and is
    what /api/bot/calibrate stores for live trading; `out_of_sample` is the
    honest estimate of how that scheme performed on data it never saw.
    """
    cfg = cfg or BacktestConfig()
    frames = _prepare(data, cfg)
    if not frames:
        raise ValueError("Not enough history for walk-forward validation")
    dates = sorted(set().union(*(f.index for f in frames.values())))
    first_tradeable = cfg.strategy.min_history + min_train_bars
    if len(dates) < first_tradeable + n_folds * 21:
        raise ValueError("Not enough history for walk-forward validation (need ~2+ years)")
    test_dates = dates[first_tradeable:]
    fold_len = len(test_dates) // n_folds

    folds, oos_trades, curve = [], [], []
    capital = cfg.initial_capital
    for k in range(n_folds):
        test_start = test_dates[k * fold_len]
        test_end = test_dates[-1] if k == n_folds - 1 else test_dates[(k + 1) * fold_len - 1]
        train = run_backtest(data, cfg, Calibrator(), end=test_start - pd.Timedelta(days=1), frames=frames, explore=True)
        calibrator = Calibrator.fit(train.trades)
        fold_cfg = BacktestConfig(**{**cfg.__dict__, "initial_capital": capital})
        test = run_backtest(data, fold_cfg, calibrator, start=test_start, end=test_end, frames=frames)
        capital = test.metrics.get("final_equity", capital)
        oos_trades.extend(test.trades)
        curve.extend(test.equity_curve)
        folds.append({
            "fold": k + 1, "test_start": str(pd.Timestamp(test_start).date()), "test_end": str(pd.Timestamp(test_end).date()),
            "calibration_trades": len(train.trades), "metrics": test.metrics, "benchmark": test.benchmark,
        })

    eq = pd.Series([r["equity"] for r in curve], index=pd.to_datetime([r["date"] for r in curve]))
    oos = performance_metrics(eq, oos_trades, pd.Series([r["exposure"] for r in curve]))
    full = run_backtest(data, cfg, Calibrator(), frames=frames, explore=True)
    final_cal = Calibrator.fit(full.trades)
    return {
        "out_of_sample": oos,
        "folds": folds,
        "equity_curve": curve,
        "calibration": final_cal.to_dict(),
        "calibration_trades": len(full.trades),
        "regime_weights": {r.value: w for r, w in REGIME_WEIGHTS.items()},
    }
