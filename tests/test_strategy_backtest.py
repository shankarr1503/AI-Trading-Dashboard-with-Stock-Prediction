import numpy as np
import pandas as pd
import pytest

from backend.trading.backtest import BacktestConfig, run_backtest, walk_forward
from backend.trading.strategy import StrategyConfig, analyze_latest, compute_factor_frame, update_trailing_stop
from tests.conftest import make_ohlcv, make_trending


def test_factor_frame_is_causal():
    df = make_ohlcv(7, n=500)
    full = compute_factor_frame(df)
    part = compute_factor_frame(df.iloc[:400])
    cols = ["trend", "momentum", "meanrev", "volume_flow", "tech_score"]
    pd.testing.assert_frame_equal(full[cols].iloc[:400], part[cols], check_exact=False, rtol=1e-9)
    assert (full["regime"].iloc[:400] == part["regime"]).all()


def test_scores_bounded_and_uptrend_is_bullish():
    frame = compute_factor_frame(make_trending(1))
    s = frame["tech_score"].dropna()
    assert s.between(-1, 1).all()
    a = analyze_latest("UP", frame)
    assert a.score > 0.25 and a.signal == "BUY"
    assert a.stop < a.price < a.target and a.reward_risk >= 1.5


def test_analysis_requires_history():
    with pytest.raises(ValueError):
        analyze_latest("X", compute_factor_frame(make_ohlcv(1, n=100)))


def test_ml_statistical_fallback_is_ignored():
    frame = compute_factor_frame(make_trending(2))
    base = analyze_latest("X", frame)
    fallback = {"source": "statistical_fallback", "predictions": {"next_day": {"bullish_probability": 0.99}}}
    assert analyze_latest("X", frame, ml=fallback).score == base.score
    real = {"source": "ml_service", "predictions": {"next_day": {"bullish_probability": 0.2}}}
    assert analyze_latest("X", frame, ml=real).score < base.score


def test_trailing_stop_only_ratchets_up():
    cfg = StrategyConfig()
    stop, high = 95.0, 100.0
    for close in [101, 104, 110, 103, 99]:
        new_stop, high = update_trailing_stop(stop, 100.0, high, close, 2.0, 2.0, 0.001, cfg)
        assert new_stop >= stop
        stop = new_stop
    assert stop >= 100.0 * 1.001  # breakeven + costs locked in after the run-up


def test_backtest_executes_next_bar_and_pays_costs():
    data = {f"S{i}": make_ohlcv(i, n=700) for i in range(4)}
    res = run_backtest(data)
    m = res.metrics
    assert m["trades"] > 0
    assert m["total_costs"] > 0
    assert m["max_drawdown_pct"] <= 0
    for t in res.trades:
        assert t["costs"] > 0
        assert t["exit_date"] >= t["entry_date"]
    assert set(res.benchmark) >= {"total_return_pct", "max_drawdown_pct"}
    assert len(res.equity_curve) > 100


def test_backtest_never_exceeds_risk_budget():
    data = {f"S{i}": make_ohlcv(i + 10, n=700) for i in range(3)}
    res = run_backtest(data)
    # A stop fill (not a gap) loses at most ~1R plus slippage/fees.
    stop_trades = [t for t in res.trades if t["exit_reason"] == "stop"]
    assert all(t["r_multiple"] > -1.2 for t in stop_trades)


def test_kill_switch_triggers_in_a_crash():
    df = make_ohlcv(3, n=600, regime_switch=False, drift=0.002, vol=0.008)
    crash = df.copy()
    factor = np.ones(len(df))
    factor[450:] = np.exp(np.cumsum(np.full(150, -0.02)))
    for col in ("open", "high", "low", "close"):
        crash[col] = crash[col] * factor
    cfg = BacktestConfig()
    cfg.risk.max_drawdown_pct = 0.03
    cfg.risk.drawdown_throttle_start_pct = 0.01
    res = run_backtest({"A": crash, "B": make_trending(4, n=600)}, cfg)
    assert res.metrics["max_drawdown_pct"] > -15  # losses contained


def test_walk_forward_out_of_sample():
    data = {f"S{i}": make_ohlcv(i, n=900) for i in range(3)}
    wf = walk_forward(data, n_folds=3)
    assert len(wf["folds"]) == 3
    assert "sharpe" in wf["out_of_sample"]
    starts = [f["test_start"] for f in wf["folds"]]
    assert starts == sorted(starts)


def test_backtest_honours_the_reentry_cooldown():
    from backend.trading.risk import RiskConfig

    data = {s: make_ohlcv(seed, n=700) for s, seed in (("A", 11), ("B", 12), ("C", 13))}

    def quick_reentries(hours):
        res = run_backtest(data, BacktestConfig(risk=RiskConfig(reentry_cooldown_hours=hours)), explore=True)
        stops = {(t["symbol"], t["exit_date"]) for t in res.trades if t["exit_reason"] in ("stop", "stop_gap")}
        quick = 0
        for t in res.trades:
            for sym, d in stops:
                gap = np.busday_count(d, t["entry_date"])
                if t["symbol"] == sym and 0 < gap <= 2:
                    quick += 1
        return quick, len(stops)

    loose, stops = quick_reentries(0)
    strict, _ = quick_reentries(72)          # 3 days → the next two signal bars are blocked
    assert stops > 0 and loose > 0
    assert strict == 0
