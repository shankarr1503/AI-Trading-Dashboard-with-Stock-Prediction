"""
Research orchestration: data fetch → fundamentals → valuation → scorecard →
report (Claude or rules-based), with caching and persistence.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.cache import cache_get, cache_set
from backend.config import settings
from backend.database.models import ResearchReport
from backend.database.session import as_utc, utcnow
from backend.market_data.service import market_data_service, validate_symbol
from backend.research import scorecard as sc
from backend.research.analyst import ClaudeAnalyst, quant_report
from backend.research.data import fetch_snapshot_sync
from backend.research.fundamentals import analyze_fundamentals
from backend.research.valuation import value_company

logger = logging.getLogger(__name__)

SNAPSHOT_TTL = 12 * 3600
DOSSIER_TTL = 3600
_FETCH_SEM = asyncio.Semaphore(6)

UNIVERSES: Dict[str, List[str]] = {
    "us_large_cap": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "AVGO", "TSLA", "LLY", "JPM", "V", "UNH",
        "XOM", "MA", "JNJ", "PG", "HD", "COST", "MRK", "ABBV", "CVX", "PEP", "KO", "WMT", "BAC", "ADBE",
        "CRM", "NFLX", "AMD", "ORCL",
    ],
    "india_large_cap": [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "HINDUNILVR.NS", "ITC.NS", "SBIN.NS",
        "BHARTIARTL.NS", "KOTAKBANK.NS", "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
        "HCLTECH.NS", "SUNPHARMA.NS", "TITAN.NS", "WIPRO.NS", "ULTRACEMCO.NS",
    ],
}


def _slim_snapshot(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot without raw statements (those are summarised by the fundamentals history)."""
    return {k: v for k, v in snap.items() if k != "statements"}


class ResearchService:
    def __init__(self, market=market_data_service, snapshot_fetcher=fetch_snapshot_sync, analyst: Optional[ClaudeAnalyst] = None):
        self.market = market
        self.snapshot_fetcher = snapshot_fetcher
        self.analyst = analyst

    async def snapshot(self, symbol: str) -> Dict[str, Any]:
        symbol = validate_symbol(symbol)
        key = f"research:snapshot:{symbol}"
        cached = await cache_get(key)
        if cached is not None:
            return cached
        async with _FETCH_SEM:
            snap = await asyncio.to_thread(self.snapshot_fetcher, symbol)
        if not snap.get("market", {}).get("price") and not snap.get("statements", {}).get("annual", {}).get("income"):
            raise ValueError(f"No fundamental data available for '{symbol}'")
        await cache_set(key, snap, SNAPSHOT_TTL)
        return snap

    async def dossier(self, symbol: str) -> Dict[str, Any]:
        """Everything the analyst needs, computed deterministically."""
        symbol = validate_symbol(symbol)
        key = f"research:dossier:{symbol}"
        cached = await cache_get(key)
        if cached is not None:
            return cached
        snap = await self.snapshot(symbol)
        try:
            prices_df = await self.market.get_history_df(symbol, period="2y", interval="1d")
        except ValueError:
            prices_df = None
        if prices_df is not None and len(prices_df) and not snap.get("market", {}).get("price"):
            snap.setdefault("market", {})["price"] = float(prices_df["close"].iloc[-1])

        fundamentals = analyze_fundamentals(snap)
        valuation = value_company(snap, fundamentals)
        stats = sc.price_stats(prices_df)
        card = sc.score(fundamentals, valuation, snap, stats)
        technical = await self._technical(symbol, prices_df)
        out = {
            "symbol": symbol,
            "as_of": utcnow().isoformat(),
            "snapshot": _slim_snapshot(snap),
            "fundamentals": fundamentals,
            "valuation": valuation,
            "price_stats": stats,
            "scorecard": card,
            "technical": technical,
        }
        await cache_set(key, out, DOSSIER_TTL)
        return out

    async def _technical(self, symbol: str, df) -> Dict[str, Any]:
        if df is None or len(df) < 220:
            return {}
        from backend.trading.strategy import StrategyConfig, analyze_latest, compute_factor_frame

        try:
            frame = await asyncio.to_thread(compute_factor_frame, df, StrategyConfig())
            a = analyze_latest(symbol, frame)
            return {"regime": a.regime, "score": a.score, "signal": a.signal, "components": a.components,
                    "atr_pct": a.atr_pct, "stop": a.stop, "target": a.target, "reasons": a.reasons}
        except ValueError as e:
            return {"error": str(e)}

    async def fundamental_view(self, symbol: str) -> Dict[str, Any]:
        """Compact view used by the trading bot (cached, never raises)."""
        try:
            d = await self.dossier(symbol)
        except Exception as e:
            return {"available": False, "reason": str(e)}
        f = d["fundamentals"]
        return {
            "available": True,
            "composite": d["scorecard"].get("composite"),
            "coverage": d["scorecard"].get("coverage"),
            "altman_zone": (f.get("altman") or {}).get("zone"),
            "piotroski": (f.get("piotroski") or {}).get("score"),
            "flags": f.get("flags", []),
            "fair_value": d["valuation"].get("fair_value"),
            "upside_pct": d["valuation"].get("upside_pct"),
            "next_earnings": d["snapshot"].get("calendar", {}).get("next_earnings"),
        }

    async def latest_report(self, db: AsyncSession, symbol: str, source: Optional[str] = None) -> Optional[ResearchReport]:
        q = select(ResearchReport).where(ResearchReport.symbol == symbol)
        if source:
            q = q.where(ResearchReport.source == source)
        return (await db.execute(q.order_by(desc(ResearchReport.created_at)).limit(1))).scalar_one_or_none()

    async def report(self, db: AsyncSession, symbol: str, allow_llm: bool, refresh: bool = False,
                     user_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Return a fresh-enough stored report, or generate one. A Claude report is
        generated only when `allow_llm` (caller is permitted) and research LLM
        is enabled; otherwise the deterministic quant report is produced.
        """
        symbol = validate_symbol(symbol)
        max_age = timedelta(hours=settings.RESEARCH_REPORT_MAX_AGE_HOURS)
        use_llm = allow_llm and settings.RESEARCH_LLM_ENABLED
        if not refresh:
            for source in (("claude", "quant_model") if not use_llm else ("claude",)):
                existing = await self.latest_report(db, symbol, source)
                if existing and utcnow() - as_utc(existing.created_at) < max_age:
                    return self._serialize(existing)

        dossier = await self.dossier(symbol)
        price = dossier["valuation"].get("price")
        source, model, usage, llm_error = "quant_model", None, None, None
        if use_llm:
            analyst = self.analyst or ClaudeAnalyst()
            try:
                rep = await analyst.write_report(symbol, price, self._tool_handler(dossier))
                usage, model = rep.pop("usage", None), rep.pop("model", None)
                source = "claude"
            except Exception as e:
                llm_error = str(e)
                logger.warning("Claude analyst failed for %s (%s); using quant report", symbol, e)
                rep = quant_report(dossier)
        else:
            rep = quant_report(dossier)
        if llm_error:
            rep.setdefault("data_gaps", []).append(f"AI analyst unavailable: {llm_error}")

        row = ResearchReport(
            symbol=symbol, source=source, model=model, rating=rep["rating"], conviction=rep.get("conviction"),
            price=price, expected_price=rep.get("expected_price"), expected_return_pct=rep.get("expected_return_pct"),
            composite_score=dossier["scorecard"].get("composite"), fair_value=dossier["valuation"].get("fair_value"),
            report=rep, usage=usage, requested_by=user_id,
        )
        db.add(row)
        await db.flush()
        return self._serialize(row)

    @staticmethod
    def _serialize(r: ResearchReport) -> Dict[str, Any]:
        return {
            "id": r.id, "symbol": r.symbol, "created_at": r.created_at, "source": r.source, "model": r.model,
            "rating": r.rating, "conviction": r.conviction, "price": r.price, "expected_price": r.expected_price,
            "expected_return_pct": r.expected_return_pct, "composite_score": r.composite_score,
            "fair_value": r.fair_value, "report": r.report, "usage": r.usage,
        }

    def _tool_handler(self, dossier: Dict[str, Any]):
        snap = dossier["snapshot"]

        async def handler(name: str, args: dict):
            if name == "get_company_profile":
                return {"profile": snap.get("profile"), "market": snap.get("market")}
            if name == "get_financials":
                return dossier["fundamentals"]
            if name == "get_valuation":
                return dossier["valuation"]
            if name == "get_street_view":
                return {"street": snap.get("street"), "insider_transactions": snap.get("insider_transactions"),
                        "calendar": snap.get("calendar"), "info_metrics": snap.get("info_metrics")}
            if name == "get_factor_scores":
                return dossier["scorecard"]
            if name == "get_technical_analysis":
                return {"technical": dossier["technical"], "price_stats": dossier["price_stats"]}
            if name == "get_recent_news":
                return {"headlines": (await self.market.get_news(dossier["symbol"]))[:15]}
            raise ValueError(f"Unknown tool {name}")

        return handler

    async def screen(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """Score every symbol (no LLM) and rank them."""
        async def one(sym: str):
            try:
                d = await self.dossier(sym)
                v, f = d["valuation"], d["fundamentals"]
                return {
                    "symbol": sym,
                    "name": d["snapshot"].get("profile", {}).get("name"),
                    "sector": d["snapshot"].get("profile", {}).get("sector"),
                    "price": v.get("price"),
                    "scores": {"composite": d["scorecard"].get("composite"),
                               **{k: x.get("score") for k, x in d["scorecard"]["factors"].items()}},
                    "coverage": d["scorecard"].get("coverage"),
                    "fair_value": v.get("fair_value"),
                    "upside_pct": v.get("upside_pct"),
                    "street_upside_pct": v.get("street", {}).get("upside_to_mean_pct"),
                    "pe": v.get("multiples", {}).get("pe"),
                    "piotroski": (f.get("piotroski") or {}).get("score"),
                    "altman_zone": (f.get("altman") or {}).get("zone"),
                    "flags": f.get("flags", []),
                    "technical_signal": d["technical"].get("signal"),
                }
            except Exception as e:
                return {"symbol": sym, "error": str(e)}

        rows = await asyncio.gather(*(one(s) for s in symbols))
        ok = [r for r in rows if "error" not in r]
        failed = [r for r in rows if "error" in r]
        return sc.rank_universe(ok) + failed


research_service = ResearchService()
