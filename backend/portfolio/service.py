"""
Portfolio Analytics Service.
Computes P&L, Sharpe ratio, max drawdown and sector allocation.
"""
import logging
import math
from typing import Any, Dict, List

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Holding, Portfolio
from backend.database.session import utcnow
from backend.market_data.service import market_data_service

logger = logging.getLogger(__name__)


class PortfolioService:
    async def get_summary(self, portfolio: Portfolio, db: AsyncSession) -> Dict[str, Any]:
        holdings = (await db.execute(select(Holding).where(Holding.portfolio_id == portfolio.id))).scalars().all()
        base = {
            "portfolio_id": portfolio.id,
            "name": portfolio.name,
            "initial_capital": portfolio.initial_capital,
            "last_updated": utcnow().isoformat(),
        }
        if not holdings:
            return {**base, "total_value": 0, "total_invested": 0, "total_pnl": 0, "total_pnl_pct": 0,
                    "sharpe_ratio": None, "max_drawdown_pct": None, "holdings": [], "sector_allocation": {}}

        quotes = await market_data_service.get_quotes(sorted({h.symbol for h in holdings}))
        rows: List[Dict[str, Any]] = []
        total_value = total_invested = 0.0
        sector_values: Dict[str, float] = {}
        for h in holdings:
            quote = quotes.get(h.symbol) or {}
            priced = "error" not in quote and quote.get("current_price")
            current_price = float(quote["current_price"]) if priced else float(h.avg_buy_price)
            market_value = h.quantity * current_price
            cost_basis = h.quantity * h.avg_buy_price
            pnl = market_value - cost_basis
            sector = h.sector or quote.get("sector") or "Other"
            rows.append({
                "symbol": h.symbol,
                "quantity": h.quantity,
                "avg_buy_price": h.avg_buy_price,
                "current_price": round(current_price, 4),
                "price_stale": not priced,
                "market_value": round(market_value, 2),
                "cost_basis": round(cost_basis, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / cost_basis * 100, 2) if cost_basis else 0.0,
                "day_change_pct": quote.get("change_pct", 0.0) if priced else None,
                "sector": sector,
            })
            total_value += market_value
            total_invested += cost_basis
            sector_values[sector] = sector_values.get(sector, 0.0) + market_value

        for r in rows:
            r["weight"] = round(r["market_value"] / total_value * 100, 2) if total_value else 0.0
        total_pnl = total_value - total_invested
        risk = await self._risk_metrics(holdings)
        return {
            **base,
            "total_value": round(total_value, 2),
            "total_invested": round(total_invested, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested else 0.0,
            "sharpe_ratio": risk.get("sharpe"),
            "max_drawdown_pct": risk.get("max_drawdown_pct"),
            "volatility_pct": risk.get("volatility_pct"),
            "holdings": rows,
            "sector_allocation": {s: round(v / total_value * 100, 2) for s, v in sector_values.items()} if total_value else {},
        }

    async def _risk_metrics(self, holdings: List[Holding], risk_free_rate: float = 0.04) -> Dict[str, float]:
        """
        Risk of the *current* portfolio over the last year: daily returns of the
        position-weighted portfolio (not a pooled average of individual stocks).
        """
        try:
            closes = {}
            for h in holdings:
                try:
                    closes[h.symbol] = (await market_data_service.get_history_df(h.symbol, period="1y"))["close"]
                except ValueError:
                    continue
            if not closes:
                return {}
            prices = pd.DataFrame(closes).ffill().dropna()
            if len(prices) < 30:
                return {}
            qty: Dict[str, float] = {}
            for h in holdings:
                if h.symbol in prices:
                    qty[h.symbol] = qty.get(h.symbol, 0.0) + h.quantity
            value = sum(prices[s] * q for s, q in qty.items())
            rets = value.pct_change().dropna()
            vol = float(rets.std() * math.sqrt(252))
            sharpe = ((float(rets.mean()) * 252 - risk_free_rate) / vol) if vol > 0 else None
            dd = float((value / value.cummax() - 1).min())
            return {
                "sharpe": round(sharpe, 3) if sharpe is not None else None,
                "volatility_pct": round(vol * 100, 2),
                "max_drawdown_pct": round(dd * 100, 2),
            }
        except Exception as e:
            logger.warning("Portfolio risk metrics failed: %s", e)
            return {}


portfolio_service = PortfolioService()
