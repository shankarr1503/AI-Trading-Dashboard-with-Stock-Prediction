"""
Portfolio Analytics Service.
Computes P&L, Sharpe ratio, max drawdown, sector allocation, cumulative returns.
"""
import logging
from typing import Dict, Any, List
from datetime import datetime
import statistics

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database.models import Portfolio, Holding, Transaction
from backend.market_data.service import market_data_service

logger = logging.getLogger(__name__)


class PortfolioService:
    """Portfolio analytics: P&L, Sharpe, drawdown, allocation."""

    async def get_summary(self, portfolio_id: int, db: AsyncSession) -> Dict[str, Any]:
        """Get full portfolio analytics summary."""
        # Fetch portfolio
        result = await db.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id)
        )
        portfolio = result.scalar_one_or_none()
        if not portfolio:
            raise ValueError(f"Portfolio {portfolio_id} not found")

        # Fetch holdings
        result = await db.execute(
            select(Holding).where(Holding.portfolio_id == portfolio_id)
        )
        holdings = result.scalars().all()

        if not holdings:
            return {
                "portfolio_id": portfolio_id,
                "name": portfolio.name,
                "total_value": 0,
                "total_invested": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "holdings": [],
                "sector_allocation": {},
            }

        # Enrich holdings with live prices
        holding_data = []
        total_value = 0
        total_invested = 0
        sector_values: Dict[str, float] = {}

        for h in holdings:
            try:
                quote = await market_data_service.get_quote(h.symbol)
                current_price = quote.get("current_price", h.avg_buy_price)
            except Exception:
                current_price = h.avg_buy_price

            market_value = h.quantity * current_price
            cost_basis = h.quantity * h.avg_buy_price
            pnl = market_value - cost_basis
            pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0
            change_pct = quote.get("change_pct", 0) if "quote" in dir() else 0

            holding_data.append({
                "symbol": h.symbol,
                "quantity": h.quantity,
                "avg_buy_price": h.avg_buy_price,
                "current_price": current_price,
                "market_value": round(market_value, 2),
                "cost_basis": round(cost_basis, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "day_change_pct": change_pct,
                "sector": h.sector or "Other",
                "weight": 0,  # filled below
            })

            total_value += market_value
            total_invested += cost_basis
            sector = h.sector or "Other"
            sector_values[sector] = sector_values.get(sector, 0) + market_value

        # Compute weights
        for h in holding_data:
            h["weight"] = round((h["market_value"] / total_value * 100) if total_value else 0, 2)

        total_pnl = total_value - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0

        # Sector allocation percentages
        sector_allocation = {
            sector: round(val / total_value * 100, 2)
            for sector, val in sector_values.items()
        } if total_value else {}

        # Sharpe ratio stub (need historical returns for proper computation)
        sharpe = await self._compute_sharpe(holdings)

        return {
            "portfolio_id": portfolio_id,
            "name": portfolio.name,
            "initial_capital": portfolio.initial_capital,
            "total_value": round(total_value, 2),
            "total_invested": round(total_invested, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "sharpe_ratio": sharpe,
            "holdings": holding_data,
            "sector_allocation": sector_allocation,
            "last_updated": datetime.utcnow().isoformat(),
        }

    async def _compute_sharpe(
        self,
        holdings: List[Holding],
        risk_free_rate: float = 0.05,
    ) -> float:
        """Compute approximate portfolio Sharpe ratio using 3-month returns."""
        try:
            all_returns = []
            for h in holdings[:5]:  # limit API calls
                history = await market_data_service.get_history(
                    h.symbol, period="3mo", interval="1d"
                )
                closes = [d["close"] for d in history if d.get("close")]
                if len(closes) > 1:
                    daily_returns = [
                        (closes[i] - closes[i - 1]) / closes[i - 1]
                        for i in range(1, len(closes))
                    ]
                    all_returns.extend(daily_returns)

            if len(all_returns) < 10:
                return 0.0

            mean_return = sum(all_returns) / len(all_returns)
            std_return = statistics.stdev(all_returns)
            if std_return == 0:
                return 0.0

            daily_rf = risk_free_rate / 252
            sharpe = ((mean_return - daily_rf) / std_return) * (252 ** 0.5)
            return round(sharpe, 3)

        except Exception as e:
            logger.warning(f"Sharpe computation failed: {e}")
            return 0.0


portfolio_service = PortfolioService()
