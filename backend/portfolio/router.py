"""Portfolio router."""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from pydantic import BaseModel
from typing import Optional

from backend.database.session import get_db
from backend.database.models import Portfolio, Holding, Transaction, TransactionType
from backend.portfolio.service import portfolio_service

router = APIRouter()


class CreatePortfolioRequest(BaseModel):
    name: str = "My Portfolio"
    description: Optional[str] = None
    initial_capital: float = 0.0
    user_id: int = 1  # simplified — in production use JWT user


class AddHoldingRequest(BaseModel):
    symbol: str
    quantity: float
    avg_buy_price: float
    sector: Optional[str] = None


@router.post("/")
async def create_portfolio(req: CreatePortfolioRequest, db: AsyncSession = Depends(get_db)):
    """Create a new portfolio."""
    portfolio = Portfolio(
        user_id=req.user_id,
        name=req.name,
        description=req.description,
        initial_capital=req.initial_capital,
    )
    db.add(portfolio)
    await db.flush()
    return {"id": portfolio.id, "name": portfolio.name, "message": "Portfolio created"}


@router.get("/{portfolio_id}/summary")
async def get_portfolio_summary(portfolio_id: int, db: AsyncSession = Depends(get_db)):
    """Get full portfolio analytics: P&L, Sharpe ratio, sector allocation."""
    try:
        return await portfolio_service.get_summary(portfolio_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Portfolio analytics failed: {e}")


@router.post("/{portfolio_id}/holdings")
async def add_holding(
    portfolio_id: int,
    req: AddHoldingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Add a stock holding to a portfolio."""
    holding = Holding(
        portfolio_id=portfolio_id,
        symbol=req.symbol.upper(),
        quantity=req.quantity,
        avg_buy_price=req.avg_buy_price,
        sector=req.sector,
    )
    db.add(holding)
    await db.flush()
    return {"id": holding.id, "symbol": holding.symbol, "message": "Holding added"}


@router.get("/{portfolio_id}/holdings")
async def list_holdings(portfolio_id: int, db: AsyncSession = Depends(get_db)):
    """List all holdings for a portfolio."""
    result = await db.execute(
        select(Holding).where(Holding.portfolio_id == portfolio_id)
    )
    return result.scalars().all()
