"""
Transaction cost model.

Every trade pays: commission, half the bid/ask spread, slippage (market impact
and latency), and regulatory/exchange levies. The bot uses this model twice:
to *refuse* trades whose expected edge doesn't clear round-trip costs with a
safety margin, and to simulate realistic fills in paper trading and backtests.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CostModel:
    name: str = "us_equity_zero_commission"
    commission_per_share: float = 0.0
    commission_pct: float = 0.0        # fraction of notional, per side
    min_commission: float = 0.0        # per order
    max_commission_pct: float = 0.01   # per-share schedules are usually capped at 1% of notional
    half_spread_bps: float = 2.0       # paid on each side when crossing the spread
    slippage_bps: float = 3.0          # adverse execution vs. reference price, per side
    buy_fee_pct: float = 0.0           # stamp duty etc. on buys
    sell_fee_pct: float = 0.0000278    # e.g. SEC Section 31 fee on US sells

    @property
    def per_side_price_impact(self) -> float:
        return (self.half_spread_bps + self.slippage_bps) / 10_000

    def commission(self, qty: float, price: float) -> float:
        notional = abs(qty) * price
        fee = abs(qty) * self.commission_per_share + notional * self.commission_pct
        if self.commission_per_share > 0:
            fee = min(fee, notional * self.max_commission_pct)
        return max(fee, self.min_commission) if qty else 0.0

    def fees(self, side: str, qty: float, price: float) -> float:
        notional = abs(qty) * price
        rate = self.buy_fee_pct if side.upper() == "BUY" else self.sell_fee_pct
        return self.commission(qty, price) + notional * rate

    def fill_price(self, side: str, ref_price: float) -> float:
        """Execution price after spread + slippage (always adverse)."""
        impact = self.per_side_price_impact
        return ref_price * (1 + impact) if side.upper() == "BUY" else ref_price * (1 - impact)

    def round_trip_cost_pct(self, price: float, qty: float = 0.0) -> float:
        """Total cost of entering and exiting, as a fraction of entry notional."""
        pct = 2 * self.per_side_price_impact + self.buy_fee_pct + self.sell_fee_pct
        if qty > 0 and price > 0:
            notional = qty * price
            pct += 2 * self.commission(qty, price) / notional
        else:
            pct += 2 * self.commission_pct
        return pct

    def to_dict(self) -> dict:
        return asdict(self)


US_EQUITY = CostModel()

# Indian equity delivery (approximation of discount-broker charges):
# STT 0.1% both sides, exchange + SEBI ≈ 0.0035%, stamp duty 0.015% on buys,
# GST on brokerage/exchange charges, wider spreads than US mega-caps.
INDIA_EQUITY_DELIVERY = CostModel(
    name="india_equity_delivery",
    commission_pct=0.0,
    min_commission=0.0,
    half_spread_bps=4.0,
    slippage_bps=5.0,
    buy_fee_pct=0.001 + 0.000035 + 0.00015,
    sell_fee_pct=0.001 + 0.000035,
)


def cost_model_for(symbol: str) -> CostModel:
    s = symbol.upper()
    if s.endswith(".NS") or s.endswith(".BO"):
        return INDIA_EQUITY_DELIVERY
    return US_EQUITY
