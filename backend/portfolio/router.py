"""Portfolio router — every endpoint is scoped to the authenticated user."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_current_user
from backend.database.models import Holding, Portfolio, User
from backend.database.session import get_db
from backend.market_data.service import validate_symbol
from backend.portfolio.service import portfolio_service

router = APIRouter()
logger = logging.getLogger(__name__)


class CreatePortfolioRequest(BaseModel):
    name: str = Field("My Portfolio", min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    initial_capital: float = Field(0.0, ge=0)


class AddHoldingRequest(BaseModel):
    symbol: str
    quantity: float = Field(gt=0)
    avg_buy_price: float = Field(gt=0)
    sector: Optional[str] = Field(None, max_length=100)


async def get_owned_portfolio(portfolio_id: int, user: User, db: AsyncSession) -> Portfolio:
    portfolio = await db.get(Portfolio, portfolio_id)
    # 404 (not 403) so other users' portfolio IDs can't be enumerated.
    if portfolio is None or portfolio.user_id != user.id:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


@router.get("/")
async def list_portfolios(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(select(Portfolio).where(Portfolio.user_id == user.id).order_by(Portfolio.id))).scalars().all()
    return [{"id": p.id, "name": p.name, "description": p.description, "initial_capital": p.initial_capital} for p in rows]


@router.post("/", status_code=201)
async def create_portfolio(req: CreatePortfolioRequest, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    portfolio = Portfolio(user_id=user.id, name=req.name, description=req.description, initial_capital=req.initial_capital)
    db.add(portfolio)
    await db.flush()
    return {"id": portfolio.id, "name": portfolio.name, "message": "Portfolio created"}


@router.get("/{portfolio_id}/summary")
async def get_portfolio_summary(portfolio_id: int, db: AsyncSession = Depends(get_db),
                                user: User = Depends(get_current_user)):
    """P&L, Sharpe ratio, max drawdown and sector allocation."""
    portfolio = await get_owned_portfolio(portfolio_id, user, db)
    try:
        return await portfolio_service.get_summary(portfolio, db)
    except Exception:
        logger.exception("Portfolio analytics failed for %s", portfolio_id)
        raise HTTPException(status_code=500, detail="Portfolio analytics failed")


@router.post("/{portfolio_id}/holdings", status_code=201)
async def add_holding(portfolio_id: int, req: AddHoldingRequest, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    await get_owned_portfolio(portfolio_id, user, db)
    try:
        symbol = validate_symbol(req.symbol)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    holding = Holding(portfolio_id=portfolio_id, symbol=symbol, quantity=req.quantity,
                      avg_buy_price=req.avg_buy_price, sector=req.sector)
    db.add(holding)
    await db.flush()
    return {"id": holding.id, "symbol": holding.symbol, "message": "Holding added"}


@router.get("/{portfolio_id}/holdings")
async def list_holdings(portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await get_owned_portfolio(portfolio_id, user, db)
    rows = (await db.execute(select(Holding).where(Holding.portfolio_id == portfolio_id))).scalars().all()
    return [{"id": h.id, "symbol": h.symbol, "quantity": h.quantity, "avg_buy_price": h.avg_buy_price, "sector": h.sector}
            for h in rows]


@router.delete("/{portfolio_id}/holdings/{holding_id}")
async def delete_holding(portfolio_id: int, holding_id: int, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    await get_owned_portfolio(portfolio_id, user, db)
    holding = await db.get(Holding, holding_id)
    if holding is None or holding.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="Holding not found")
    await db.delete(holding)
    return {"message": "Holding removed"}
