"""
Risk manager: the bot's final, deterministic authority on every entry.

Nothing — not the strategy, not the ML model, not the LLM reviewer — can place
a trade that fails these checks. The checks exist to keep any single trade,
any single day and any losing streak from doing serious damage, and to refuse
trades whose expected edge doesn't clear transaction costs.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from backend.trading.calibration import EdgeEstimate
from backend.trading.costs import CostModel
from backend.trading.regime import Regime


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 0.01      # lose at most 1% of equity if the stop is hit
    kelly_scale: float = 0.25                 # quarter-Kelly
    max_position_pct: float = 0.15            # single-name concentration cap
    max_gross_exposure_pct: float = 0.90      # no leverage; keep a cash buffer
    max_open_positions: int = 8
    daily_loss_limit_pct: float = 0.02        # stop opening trades for the day after −2%
    max_drawdown_pct: float = 0.10            # kill switch at −10% from the high-water mark
    drawdown_throttle_start_pct: float = 0.04 # start shrinking size after −4%
    cost_safety_multiple: float = 2.0         # expected edge must be ≥ 2× round-trip costs
    min_ev_r: float = 0.10                    # minimum expectancy per trade, in R
    min_reward_risk: float = 1.5
    min_notional: float = 200.0
    max_correlation: float = 0.85
    max_correlated_positions: int = 2
    high_vol_size_mult: float = 0.5
    max_consecutive_losses: int = 4
    cooldown_hours: float = 24.0
    flatten_on_kill: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    # Bounds for values an administrator may change at runtime.
    BOUNDS = {
        "max_risk_per_trade_pct": (0.001, 0.02),
        "kelly_scale": (0.05, 0.5),
        "max_position_pct": (0.02, 0.25),
        "max_gross_exposure_pct": (0.1, 1.0),
        "max_open_positions": (1, 20),
        "daily_loss_limit_pct": (0.005, 0.05),
        "max_drawdown_pct": (0.03, 0.25),
        "drawdown_throttle_start_pct": (0.01, 0.2),
        "cost_safety_multiple": (1.5, 10.0),
        "min_ev_r": (0.05, 1.0),
        "min_reward_risk": (1.0, 5.0),
        "min_notional": (50.0, 100_000.0),
        "max_correlation": (0.5, 0.99),
        "max_correlated_positions": (0, 10),
        "high_vol_size_mult": (0.1, 1.0),
        "max_consecutive_losses": (2, 20),
        "cooldown_hours": (1.0, 168.0),
    }

    def with_overrides(self, overrides: Optional[dict]) -> "RiskConfig":
        """Apply validated overrides; values outside BOUNDS raise ValueError."""
        if not overrides:
            return self
        data = self.to_dict()
        for key, value in overrides.items():
            if key == "flatten_on_kill":
                data[key] = bool(value)
                continue
            if key not in self.BOUNDS:
                raise ValueError(f"Unknown or non-editable risk setting: {key}")
            lo, hi = self.BOUNDS[key]
            if not (lo <= float(value) <= hi):
                raise ValueError(f"{key} must be between {lo} and {hi}")
            data[key] = type(data[key])(value)
        return RiskConfig(**data)


@dataclass
class PortfolioSnapshot:
    equity: float
    cash: float
    positions: Dict[str, float]          # symbol → current market value
    high_water_mark: float
    day_start_equity: float
    consecutive_losses: int = 0
    cooldown_active: bool = False

    @property
    def gross_exposure(self) -> float:
        return sum(abs(v) for v in self.positions.values())

    @property
    def drawdown_pct(self) -> float:
        if self.high_water_mark <= 0:
            return 0.0
        return max(0.0, 1 - self.equity / self.high_water_mark)

    @property
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.equity / self.day_start_equity - 1


@dataclass
class TradeProposal:
    symbol: str
    price: float
    stop: float
    target: float
    score: float
    regime: str
    edge: EdgeEstimate
    cost_model: CostModel


@dataclass
class RiskDecision:
    approved: bool
    qty: int = 0
    reasons: List[str] = field(default_factory=list)
    risk_amount: float = 0.0
    risk_pct_of_equity: float = 0.0
    expected_edge_pct: float = 0.0
    cost_pct: float = 0.0
    net_edge_pct: float = 0.0
    notional: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BreakerStatus:
    allow_entries: bool
    kill: bool
    reasons: List[str]


class RiskManager:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()

    def circuit_breakers(self, snap: PortfolioSnapshot) -> BreakerStatus:
        c = self.config
        reasons: List[str] = []
        kill = snap.drawdown_pct >= c.max_drawdown_pct
        if kill:
            reasons.append(
                f"KILL SWITCH: drawdown {snap.drawdown_pct:.1%} ≥ limit {c.max_drawdown_pct:.1%}"
            )
        if snap.daily_pnl_pct <= -c.daily_loss_limit_pct:
            reasons.append(f"Daily loss {snap.daily_pnl_pct:.2%} hit limit −{c.daily_loss_limit_pct:.1%}")
        if snap.cooldown_active:
            reasons.append(f"Cooling down after {snap.consecutive_losses} consecutive losses")
        return BreakerStatus(allow_entries=not reasons, kill=kill, reasons=reasons)

    def size_multiplier(self, snap: PortfolioSnapshot, regime: str) -> float:
        c = self.config
        mult = c.high_vol_size_mult if regime == Regime.HIGH_VOLATILITY.value else 1.0
        dd = snap.drawdown_pct
        if dd > c.drawdown_throttle_start_pct:
            span = max(1e-9, c.max_drawdown_pct - c.drawdown_throttle_start_pct)
            mult *= max(0.25, 1 - 0.75 * (dd - c.drawdown_throttle_start_pct) / span)
        return mult

    def evaluate(
        self,
        p: TradeProposal,
        snap: PortfolioSnapshot,
        correlations: Optional[Dict[str, float]] = None,
        reserved_cash: float = 0.0,
        pending_positions: int = 0,
    ) -> RiskDecision:
        c = self.config
        reasons: List[str] = []

        def reject(msg: str, **kw) -> RiskDecision:
            reasons.append(msg)
            return RiskDecision(approved=False, reasons=reasons, **kw)

        breakers = self.circuit_breakers(snap)
        if not breakers.allow_entries:
            return reject("; ".join(breakers.reasons))
        if p.symbol in snap.positions:
            return reject("Already holding this symbol")
        if len(snap.positions) + pending_positions >= c.max_open_positions:
            return reject(f"Max open positions ({c.max_open_positions}) reached")
        if not (p.price > 0 and 0 < p.stop < p.price and p.target > p.price):
            return reject("Invalid entry/stop/target geometry")

        cm = p.cost_model
        entry_fill = cm.fill_price("BUY", p.price)
        stop_fill = cm.fill_price("SELL", p.stop)
        risk_per_share = entry_fill - stop_fill
        reward_risk = (p.target - p.price) / (p.price - p.stop)
        if reward_risk < c.min_reward_risk:
            return reject(f"Reward:risk {reward_risk:.2f} < {c.min_reward_risk}")

        ev_r = p.edge.ev_r
        if ev_r < c.min_ev_r:
            return reject(f"Expectancy {ev_r:+.3f}R < minimum {c.min_ev_r}R ({p.edge.source} edge estimate)")
        kelly = p.edge.kelly
        if kelly <= 0:
            return reject("Kelly fraction ≤ 0: no positive edge")

        # ── Position sizing ──
        risk_pct = min(c.max_risk_per_trade_pct, c.kelly_scale * kelly) * self.size_multiplier(snap, p.regime)
        risk_budget = snap.equity * risk_pct
        qty_risk = risk_budget / risk_per_share
        qty_conc = c.max_position_pct * snap.equity / entry_fill
        buy_cost_factor = 1 + cm.buy_fee_pct + cm.commission_pct
        qty_cash = max(0.0, snap.cash - reserved_cash) / (entry_fill * buy_cost_factor)
        qty_gross = max(0.0, c.max_gross_exposure_pct * snap.equity - snap.gross_exposure - reserved_cash) / entry_fill
        limits = {"risk": qty_risk, "concentration": qty_conc, "cash": qty_cash, "gross_exposure": qty_gross}

        corr = correlations or {}
        correlated = [s for s, v in corr.items() if v is not None and v >= c.max_correlation]
        if correlated and len(correlated) >= c.max_correlated_positions:
            return reject(f"Too correlated with existing positions: {', '.join(correlated)}")
        if correlated:
            limits["risk"] *= 0.5
            reasons.append(f"Size halved: correlated with {', '.join(correlated)}")

        binding = min(limits, key=limits.get)
        qty = int(math.floor(limits[binding]))
        if qty <= 0:
            return reject(f"Position size rounds to 0 (binding limit: {binding})")
        notional = qty * entry_fill
        if notional < c.min_notional:
            return reject(f"Notional ${notional:,.0f} below minimum ${c.min_notional:,.0f}")

        # ── Cost gate: the edge must comfortably exceed what the trade costs ──
        cost_pct = cm.round_trip_cost_pct(p.price, qty)
        expected_edge_pct = ev_r * (risk_per_share / p.price)
        net_edge_pct = expected_edge_pct - cost_pct
        common = dict(
            expected_edge_pct=round(expected_edge_pct, 6), cost_pct=round(cost_pct, 6),
            net_edge_pct=round(net_edge_pct, 6),
        )
        if expected_edge_pct < c.cost_safety_multiple * cost_pct:
            return reject(
                f"Edge {expected_edge_pct:.3%} < {c.cost_safety_multiple}× round-trip cost {cost_pct:.3%}",
                **common,
            )

        risk_amount = qty * risk_per_share
        reasons.append(
            f"Approved {qty} sh (binding: {binding}); risk ${risk_amount:,.0f} "
            f"({risk_amount / snap.equity:.2%} of equity); edge {expected_edge_pct:.2%} vs cost {cost_pct:.2%}"
        )
        return RiskDecision(
            approved=True, qty=qty, reasons=reasons, risk_amount=round(risk_amount, 2),
            risk_pct_of_equity=round(risk_amount / snap.equity, 6), notional=round(notional, 2), **common,
        )
