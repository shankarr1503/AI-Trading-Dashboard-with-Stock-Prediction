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
from backend.research.service import UNIVERSES, SnapshotUnavailable, research_service

router = APIRouter()
logger = logging.getLogger(__name__)

# Screens fan out into one ~15-request Yahoo fetch per uncached symbol, so
# non-admins may only screen symbols from the predefined universes, and a
# custom list of them is capped. A named universe (curated, or the operator's
# BOT_UNIVERSE, which the bot keeps cached anyway) is screened whole: its size
# is set by the operator, not by the caller.
ADMIN_MAX_SCREEN_SYMBOLS = 150
NON_ADMIN_MAX_SCREEN_SYMBOLS = 40


def _not_available(e: ValueError) -> HTTPException:
    """A failed fetch (Yahoo down / rate limited) is temporary (503); no data or a bad symbol is 404."""
    if isinstance(e, SnapshotUnavailable) and e.transient:
        return HTTPException(status_code=503, detail=f"{e}. Try again in a few minutes.")
    return HTTPException(status_code=404, detail=str(e))


def screenable_symbols() -> set:
    """Symbols a non-admin may screen: the predefined universes (incl. the bot's)."""
    allowed = {s for symbols in UNIVERSES.values() for s in symbols}
    return allowed | set(settings.bot_universe)


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
        raise _not_available(e)


def _with_capabilities(result: dict, user: User) -> dict:
    return {**result, "ai_generation_available": bool(user.is_superuser and settings.RESEARCH_LLM_ENABLED)}


@router.get("/{symbol}/report")
@limiter.limit("10/minute")
async def report(
    request: Request, symbol: str,
    refresh: bool = Query(False, description="Generate a new report even if a recent one exists "
                                             "(for administrators this runs the paid AI analyst)"),
    force: bool = Query(False, description="With refresh: run the AI analyst even if it produced a report "
                                           "for this symbol in the last few minutes"),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """
    Analyst report. A plain GET (page load) never runs the paid AI analyst: it
    returns the latest stored report or a free rules-based one. Claude-written
    reports cost API credits, so only administrators can generate them, and
    only explicitly (refresh=true or POST), when RESEARCH_LLM_ENABLED=true.
    """
    try:
        result = await research_service.report(db, symbol, allow_llm=user.is_superuser and refresh,
                                               refresh=refresh, user_id=user.id, force=force)
    except ValueError as e:
        raise _not_available(e)
    return _with_capabilities(result, user)


@router.post("/{symbol}/report")
@limiter.limit("5/minute")
async def generate_report(
    request: Request, symbol: str,
    force: bool = Query(False, description="Run the AI analyst even if it produced a report for this symbol in the "
                                           "last few minutes (otherwise that report is returned, `reused_recent`)"),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Explicitly generate a new report (the paid AI analyst for administrators, rules-based otherwise)."""
    try:
        result = await research_service.report(db, symbol, allow_llm=user.is_superuser, refresh=True, user_id=user.id,
                                               force=force)
    except ValueError as e:
        raise _not_available(e)
    return _with_capabilities(result, user)


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
    symbols: Optional[List[str]] = Field(default=None, max_length=ADMIN_MAX_SCREEN_SYMBOLS)
    universe: Optional[str] = None


@router.post("/screen")
@limiter.limit("4/minute")
async def screen(request: Request, body: ScreenRequest, user: User = Depends(get_current_user)):
    """
    Rank a universe by the multi-factor scorecard (no AI cost). Administrators
    may screen any symbols; other users may screen a predefined universe whole
    (bot_universe included, whatever its configured size) or a custom subset of
    them of at most NON_ADMIN_MAX_SCREEN_SYMBOLS symbols.
    """
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
    if not user.is_superuser:
        outside = [s for s in symbols if s not in screenable_symbols()]
        if outside:
            raise HTTPException(status_code=403, detail=(
                "Custom symbols can only be screened by an administrator; choose symbols from the predefined "
                f"universes. Not allowed: {', '.join(outside[:10])}{' …' if len(outside) > 10 else ''}"))
        if body.symbols and len(symbols) > NON_ADMIN_MAX_SCREEN_SYMBOLS:
            raise HTTPException(status_code=422, detail=f"At most {NON_ADMIN_MAX_SCREEN_SYMBOLS} custom symbols per "
                                                        f"screen (or choose a predefined universe)")
    rows = await research_service.screen(symbols)
    return {"count": len(rows), "results": rows}
