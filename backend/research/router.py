"""Equity research API."""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_current_user
from backend.config import settings
from backend.database.models import ResearchReport, User
from backend.database.session import get_db
from backend.market_data.service import validate_symbol
from backend.ratelimit import limiter
from backend.research.service import UNIVERSES, research_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/universes")
async def universes(user: User = Depends(get_current_user)):
    return {"universes": {**UNIVERSES, "bot_universe": settings.bot_universe},
            "note": "Curated large-cap lists for convenience, not index replicas. Pass your own symbols to /screen."}


@router.get("/{symbol}/fundamentals")
@limiter.limit("30/minute")
async def fundamentals(request: Request, symbol: str, user: User = Depends(get_current_user)):
    """Financial analysis, valuation, factor scorecard and technical view (deterministic, no AI)."""
    try:
        return await research_service.dossier(symbol)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{symbol}/report")
@limiter.limit("10/minute")
async def report(
    request: Request, symbol: str,
    refresh: bool = Query(False, description="Generate a new report even if a recent one exists"),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """
    Analyst report. Claude-written reports cost API credits, so only administrators
    can generate new ones (when RESEARCH_LLM_ENABLED=true); everyone can read the
    latest stored report or get a free rules-based report.
    """
    try:
        return await research_service.report(db, symbol, allow_llm=user.is_superuser, refresh=refresh, user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/reports")
async def list_reports(limit: int = Query(50, le=200), db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Latest report per symbol."""
    latest = (select(ResearchReport.symbol, func.max(ResearchReport.created_at).label("ts"))
              .group_by(ResearchReport.symbol).subquery())
    rows = (await db.execute(
        select(ResearchReport).join(latest, (ResearchReport.symbol == latest.c.symbol) & (ResearchReport.created_at == latest.c.ts))
        .order_by(desc(ResearchReport.created_at)).limit(limit)
    )).scalars().all()
    return [{"symbol": r.symbol, "created_at": r.created_at, "source": r.source, "rating": r.rating,
             "conviction": r.conviction, "price": r.price, "expected_return_pct": r.expected_return_pct,
             "composite_score": r.composite_score} for r in rows]


class ScreenRequest(BaseModel):
    symbols: Optional[List[str]] = Field(default=None, max_length=150)
    universe: Optional[str] = None


@router.post("/screen")
@limiter.limit("4/minute")
async def screen(request: Request, body: ScreenRequest, user: User = Depends(get_current_user)):
    """Rank a universe by the multi-factor scorecard (no AI cost)."""
    if body.symbols:
        try:
            symbols = sorted({validate_symbol(s) for s in body.symbols})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    elif body.universe == "bot_universe":
        symbols = settings.bot_universe
    elif body.universe in UNIVERSES:
        symbols = UNIVERSES[body.universe]
    else:
        raise HTTPException(status_code=422, detail=f"Provide symbols or one of: {', '.join([*UNIVERSES, 'bot_universe'])}")
    rows = await research_service.screen(symbols)
    return {"count": len(rows), "results": rows}
