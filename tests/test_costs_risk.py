import pytest

from backend.trading.calibration import Calibrator, EdgeEstimate
from backend.trading.costs import INDIA_EQUITY_DELIVERY, US_EQUITY, CostModel, cost_model_for
from backend.trading.risk import PortfolioSnapshot, RiskConfig, RiskManager, TradeProposal


def test_fills_are_always_adverse():
    cm = US_EQUITY
    assert cm.fill_price("BUY", 100) > 100
    assert cm.fill_price("SELL", 100) < 100


def test_round_trip_cost_includes_commission_and_fees():
    cm = CostModel(commission_per_share=0.005, min_commission=1.0, half_spread_bps=2, slippage_bps=3)
    small = cm.round_trip_cost_pct(10.0, 10)      # $100 trade: min commission dominates
    large = cm.round_trip_cost_pct(10.0, 10_000)
    assert small > large > 0.001
    assert cost_model_for("TCS.NS") is INDIA_EQUITY_DELIVERY
    assert INDIA_EQUITY_DELIVERY.round_trip_cost_pct(100) > US_EQUITY.round_trip_cost_pct(100)


def snap(**kw):
    base = dict(equity=100_000, cash=100_000, positions={}, high_water_mark=100_000, day_start_equity=100_000)
    base.update(kw)
    return PortfolioSnapshot(**base)


def proposal(symbol="AAA", price=100.0, stop=94.0, target=112.0, score=0.6, edge=None, regime="BULL_TREND"):
    edge = edge or EdgeEstimate(p_win=0.5, avg_win_r=1.8, avg_loss_r=1.0, n_trades=100, source="calibrated")
    return TradeProposal(symbol, price, stop, target, score, regime, edge, US_EQUITY)


def test_sizing_never_risks_more_than_limit():
    rm = RiskManager(RiskConfig(max_risk_per_trade_pct=0.01))
    d = rm.evaluate(proposal(), snap())
    assert d.approved
    assert d.risk_amount <= 100_000 * 0.01 + 1e-6
    assert d.notional <= 100_000 * RiskConfig().max_position_pct + 1e-6


def test_rejects_when_edge_does_not_cover_costs():
    expensive = CostModel(half_spread_bps=60, slippage_bps=60)
    p = proposal()
    p.cost_model = expensive
    d = RiskManager().evaluate(p, snap())
    assert not d.approved
    assert "round-trip cost" in d.reasons[-1]


def test_rejects_negative_expectancy():
    bad = EdgeEstimate(p_win=0.30, avg_win_r=1.5, avg_loss_r=1.0, n_trades=200, source="calibrated")
    d = RiskManager().evaluate(proposal(edge=bad), snap())
    assert not d.approved and "Expectancy" in d.reasons[0]


def test_kill_switch_and_daily_loss():
    rm = RiskManager(RiskConfig(max_drawdown_pct=0.10, daily_loss_limit_pct=0.02))
    status = rm.circuit_breakers(snap(equity=89_000, high_water_mark=100_000, day_start_equity=89_500))
    assert status.kill and not status.allow_entries
    status = rm.circuit_breakers(snap(equity=97_500, day_start_equity=100_000))
    assert not status.kill and not status.allow_entries
    assert not rm.evaluate(proposal(), snap(equity=97_500, day_start_equity=100_000)).approved


def test_drawdown_throttle_shrinks_size():
    rm = RiskManager()
    full = rm.evaluate(proposal(), snap())
    throttled = rm.evaluate(proposal(), snap(equity=93_000, cash=93_000, high_water_mark=100_000, day_start_equity=93_000))
    assert throttled.approved and throttled.qty < full.qty


def test_position_limits_and_correlation():
    rm = RiskManager(RiskConfig(max_open_positions=2))
    held = snap(positions={"X": 10_000, "Y": 10_000}, cash=80_000)
    assert not rm.evaluate(proposal(), held).approved
    rm = RiskManager()
    one = snap(positions={"X": 10_000}, cash=90_000)
    halved = rm.evaluate(proposal(), one, correlations={"X": 0.95})
    normal = rm.evaluate(proposal(), one, correlations={"X": 0.1})
    assert halved.approved and halved.qty < normal.qty
    two = snap(positions={"X": 10_000, "Y": 10_000}, cash=80_000)
    assert not rm.evaluate(proposal(), two, correlations={"X": 0.9, "Y": 0.9}).approved


def test_high_vol_regime_halves_risk():
    rm = RiskManager()
    a = rm.evaluate(proposal(), snap())
    b = rm.evaluate(proposal(regime="HIGH_VOLATILITY"), snap())
    assert b.qty < a.qty


def test_config_overrides_are_bounded():
    cfg = RiskConfig().with_overrides({"max_risk_per_trade_pct": 0.005})
    assert cfg.max_risk_per_trade_pct == 0.005
    with pytest.raises(ValueError):
        RiskConfig().with_overrides({"max_risk_per_trade_pct": 0.5})
    with pytest.raises(ValueError):
        RiskConfig().with_overrides({"made_up": 1})


def test_calibrator_shrinks_small_samples():
    lucky = Calibrator.fit([{"entry_score": 0.6, "r_multiple": 2.0}] * 3)
    est = lucky.estimate(0.6)
    assert est.source == "calibrated"
    assert est.p_win < 0.7          # 3 wins out of 3 must not mean 100%
    many_losses = Calibrator.fit([{"entry_score": 0.6, "r_multiple": -1.0}] * 300)
    assert many_losses.estimate(0.6).ev_r < 0
    assert Calibrator().estimate(0.6).source == "prior"
