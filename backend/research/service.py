"""
Research orchestration: data fetch → fundamentals → valuation → scorecard →
report (Claude or rules-based), with caching and persistence.

Fetch discipline (a snapshot is ~15 Yahoo requests):
* one in-flight fetch per symbol, run as a background task: callers await it
  through asyncio.shield, so a caller's timeout never cancels (and so never
  wastes) the fetch, and the result is always cached when it lands;
* failures are negatively cached, so a bad or unknown ticker is not re-fetched
  on every request;
* a snapshot with a section that FAILED (rate limit, network) is cached briefly,
  so e.g. a missing earnings date is retried soon rather than in 12 hours;
* `cached_fundamental_view` never fetches — for anonymous/public callers.

Paid Claude reports run only when explicitly requested, one at a time per
symbol, and a failed run backs off instead of re-running on every page view.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.cache import cache_get, cache_set
from backend.config import settings
from backend.database.models import ResearchReport
from backend.database.session import as_utc, utcnow
from backend.market_data.service import market_data_service, validate_symbol
from backend.research import scorecard as sc
from backend.research.analyst import ClaudeAnalyst, quant_report
from backend.research.data import failed_sections, fetch_snapshot_sync
from backend.research.fundamentals import analyze_fundamentals
from backend.research.valuation import value_company

logger = logging.getLogger(__name__)

SNAPSHOT_TTL = 12 * 3600
DEGRADED_SNAPSHOT_TTL = 15 * 60     # a section failed: retry soon
NEGATIVE_TTL = 30 * 60              # whole fetch failed / no data: don't hammer Yahoo
DOSSIER_TTL = 3600
FETCH_CONCURRENCY = 6
LLM_FAILURE_BACKOFF = timedelta(hours=6)

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


def _snapshot_key(symbol: str) -> str:
    return f"research:snapshot:{symbol}"


def _failure_key(symbol: str) -> str:
    return f"research:snapshot-failed:{symbol}"


def _dossier_key(symbol: str) -> str:
    return f"research:dossier:{symbol}"


def with_live_price(snap: Dict[str, Any], prices_df) -> Dict[str, Any]:
    """
    The snapshot (and its price) can be up to 12h old; the daily-bar history is
    fresh. Use the latest close for price and re-derive market cap, without
    mutating the (possibly shared/cached) snapshot.
    """
    if prices_df is None or not len(prices_df):
        return snap
    try:
        close = float(prices_df["close"].iloc[-1])
    except (KeyError, TypeError, ValueError):
        return snap
    if not close > 0:
        return snap
    market = dict(snap.get("market") or {})
    old_price, old_cap, shares = market.get("price"), market.get("market_cap"), market.get("shares_outstanding")
    market["snapshot_price"] = old_price
    market["price"] = close
    market["price_source"] = "last_close"
    try:
        market["price_as_of"] = prices_df.index[-1].date().isoformat()
    except AttributeError:
        market["price_as_of"] = None
    if shares:
        market["market_cap"] = shares * close
    elif old_cap and old_price:
        market["market_cap"] = old_cap * close / old_price
    return {**snap, "market": market}


class ResearchService:
    def __init__(self, market=market_data_service, snapshot_fetcher=fetch_snapshot_sync, analyst: Optional[ClaudeAnalyst] = None):
        self.market = market
        self.snapshot_fetcher = snapshot_fetcher
        self.analyst = analyst
        # asyncio primitives are bound to the loop that first uses them; keep
        # them per loop (tests and worker restarts create new loops).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._inflight: Dict[str, asyncio.Task] = {}
        self._llm_locks: Dict[str, asyncio.Lock] = {}
        self._fetch_sem: Optional[asyncio.Semaphore] = None
        self._llm_recent: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._llm_failed_at: Dict[str, float] = {}

    def _loop_state(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._inflight = {}
            self._llm_locks = {}
            self._fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    # ─── Snapshot ─────────────────────────────────────────────────────────────

    async def snapshot(self, symbol: str) -> Dict[str, Any]:
        symbol = validate_symbol(symbol)
        cached = await cache_get(_snapshot_key(symbol))
        if cached is not None:
            return cached
        failure = await cache_get(_failure_key(symbol))
        if failure is not None:
            raise ValueError(failure.get("error") or f"No fundamental data available for '{symbol}'")
        self._loop_state()
        task = self._inflight.get(symbol)
        if task is None:
            task = asyncio.create_task(self._fetch_snapshot(symbol), name=f"research-snapshot-{symbol}")
            self._inflight[symbol] = task
            task.add_done_callback(functools.partial(self._fetch_done, symbol))
        # shield: a caller that times out (e.g. the bot's 30s budget) abandons
        # the wait, not the fetch, which still completes and is cached.
        return await asyncio.shield(task)

    def _fetch_done(self, symbol: str, task: asyncio.Task) -> None:
        if self._inflight.get(symbol) is task:
            self._inflight.pop(symbol, None)
        if not task.cancelled():
            task.exception()  # mark retrieved: callers that timed out never will

    async def _fetch_snapshot(self, symbol: str) -> Dict[str, Any]:
        try:
            async with self._fetch_sem:
                snap = await asyncio.to_thread(self.snapshot_fetcher, symbol)
            if not snap.get("market", {}).get("price") and not snap.get("statements", {}).get("annual", {}).get("income"):
                raise ValueError(f"No fundamental data available for '{symbol}'")
        except Exception as e:  # noqa: BLE001 - every failure is negatively cached
            msg = str(e) if isinstance(e, ValueError) else f"Fundamental data fetch failed for '{symbol}' ({type(e).__name__})"
            logger.info("Research snapshot for %s failed: %s", symbol, msg)
            await cache_set(_failure_key(symbol), {"error": msg}, NEGATIVE_TTL)
            raise ValueError(msg) from e
        failed = failed_sections(snap)
        if failed:
            logger.info("Research snapshot for %s is degraded (%s); caching for %ss", symbol, failed, DEGRADED_SNAPSHOT_TTL)
        await cache_set(_snapshot_key(symbol), snap, DEGRADED_SNAPSHOT_TTL if failed else SNAPSHOT_TTL)
        return snap

    # ─── Dossier ──────────────────────────────────────────────────────────────

    async def dossier(self, symbol: str, allow_fetch: bool = True) -> Dict[str, Any]:
        """
        Everything the analyst needs, computed deterministically. With
        allow_fetch=False only an already-cached snapshot is used (LookupError
        otherwise) — no research request is made.
        """
        symbol = validate_symbol(symbol)
        key = _dossier_key(symbol)
        cached = await cache_get(key)
        if cached is not None:
            return cached
        if allow_fetch:
            snap = await self.snapshot(symbol)
        else:
            snap = await cache_get(_snapshot_key(symbol))
            if snap is None:
                raise LookupError(f"Fundamentals for '{symbol}' are not cached")
        try:
            prices_df = await self.market.get_history_df(symbol, period="2y", interval="1d")
        except ValueError:
            prices_df = None
        snap = with_live_price(snap, prices_df)

        fundamentals = analyze_fundamentals(snap)
        valuation = value_company(snap, fundamentals)
        stats = sc.price_stats(prices_df)
        card = sc.score(fundamentals, valuation, snap, stats)
        technical = await self._technical(symbol, prices_df)
        failed = failed_sections(snap)
        out = {
            "symbol": symbol,
            "as_of": utcnow().isoformat(),
            "snapshot": _slim_snapshot(snap),
            "fundamentals": fundamentals,
            "valuation": valuation,
            "price_stats": stats,
            "scorecard": card,
            "technical": technical,
            "data_quality": {"failed_sections": failed, "degraded": bool(failed)},
        }
        await cache_set(key, out, min(DOSSIER_TTL, DEGRADED_SNAPSHOT_TTL) if failed else DOSSIER_TTL)
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

    # ─── Compact views for the bot / signal engine ────────────────────────────

    @staticmethod
    def _view(d: Dict[str, Any]) -> Dict[str, Any]:
        f = d["fundamentals"]
        v = d["valuation"]
        cal = d["snapshot"].get("calendar") or {}
        start = cal.get("next_earnings")
        end = cal.get("next_earnings_end") or start
        failed = (d.get("data_quality") or {}).get("failed_sections")
        if failed is None:
            failed = failed_sections(d["snapshot"])
        altman = f.get("altman") or {}
        return {
            "available": True,
            "composite": d["scorecard"].get("composite"),
            "coverage": d["scorecard"].get("coverage"),
            "altman_zone": altman.get("zone"),
            "altman_model": altman.get("model"),
            "piotroski": (f.get("piotroski") or {}).get("score"),
            "flags": f.get("flags", []),
            "fair_value": v.get("fair_value"),
            "upside_pct": v.get("upside_pct"),
            # ISO dates; an unconfirmed date is a [start, end] window (end == start when confirmed).
            "next_earnings": start,
            "next_earnings_end": end,
            # True when the earnings date is missing because the calendar fetch FAILED
            # (not because none is scheduled): callers should treat the date as unknown.
            "earnings_unknown": start is None and "calendar" in failed,
            "data_degraded": bool(failed),
            "currency_mismatch": bool((f.get("currency") or v.get("currency") or {}).get("mismatch")),
            "business_model": (f.get("business_model") or {}).get("type"),
        }

    async def fundamental_view(self, symbol: str) -> Dict[str, Any]:
        """Compact view used by the trading bot (cached, never raises; may fetch)."""
        try:
            d = await self.dossier(symbol)
        except Exception as e:
            return {"available": False, "reason": str(e)}
        return self._view(d)

    async def cached_fundamental_view(self, symbol: str) -> Dict[str, Any]:
        """Like fundamental_view but NEVER triggers a research fetch (for public endpoints)."""
        try:
            d = await self.dossier(symbol, allow_fetch=False)
        except LookupError:
            return {"available": False, "reason": "Fundamentals not loaded yet (open the research view to fetch them)"}
        except Exception as e:
            return {"available": False, "reason": str(e)}
        return self._view(d)

    # ─── Reports ──────────────────────────────────────────────────────────────

    async def latest_report(self, db: AsyncSession, symbol: str, source: Optional[str] = None) -> Optional[ResearchReport]:
        q = select(ResearchReport).where(ResearchReport.symbol == symbol)
        if source:
            q = q.where(ResearchReport.source == source)
        return (await db.execute(q.order_by(desc(ResearchReport.created_at)).limit(1))).scalar_one_or_none()

    async def _fresh_report(self, db: AsyncSession, symbol: str, source: str, max_age: timedelta) -> Optional[ResearchReport]:
        existing = await self.latest_report(db, symbol, source)
        if existing and utcnow() - as_utc(existing.created_at) < max_age:
            return existing
        return None

    async def llm_backoff_active(self, db: AsyncSession, symbol: str) -> bool:
        """A Claude run for `symbol` failed within LLM_FAILURE_BACKOFF (and none succeeded since)."""
        failed_at = self._llm_failed_at.get(symbol)
        if failed_at is not None and time.monotonic() - failed_at < LLM_FAILURE_BACKOFF.total_seconds():
            return True
        rows = (await db.execute(select(ResearchReport).where(ResearchReport.symbol == symbol)
                                 .order_by(desc(ResearchReport.created_at)).limit(10))).scalars().all()
        cutoff = utcnow() - LLM_FAILURE_BACKOFF
        for r in rows:
            if as_utc(r.created_at) < cutoff:
                break
            if r.source == "claude":
                return False
            if (r.report or {}).get("llm_failed"):
                return True
        return False

    async def report(self, db: AsyncSession, symbol: str, allow_llm: bool, refresh: bool = False,
                     user_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Return a fresh-enough stored report, or generate one.

        A Claude report is generated only when `allow_llm` (caller is permitted)
        and research LLM is enabled. Without `refresh`, a recent stored report
        (Claude preferred) is returned; a Claude run that failed within the
        backoff window is not retried automatically. `refresh` forces a new
        report (and overrides the backoff). Concurrent Claude runs for one
        symbol are de-duplicated: waiters get the report the first run produced.
        """
        symbol = validate_symbol(symbol)
        max_age = timedelta(hours=settings.RESEARCH_REPORT_MAX_AGE_HOURS)
        use_llm = allow_llm and settings.RESEARCH_LLM_ENABLED
        if not refresh:
            existing = await self._fresh_report(db, symbol, "claude", max_age)
            if existing:
                return self._serialize(existing)
            if use_llm and await self.llm_backoff_active(db, symbol):
                use_llm = False
            if not use_llm:
                existing = await self._fresh_report(db, symbol, "quant_model", max_age)
                if existing:
                    return self._serialize(existing)
        if not use_llm:
            return await self._generate(db, symbol, use_llm=False, user_id=user_id)

        self._loop_state()
        lock = self._llm_locks.setdefault(symbol, asyncio.Lock())
        requested = time.monotonic()
        async with lock:
            recent = self._llm_recent.get(symbol)
            if recent and recent[0] >= requested:
                return recent[1]  # a concurrent request for this symbol just produced a report
            if not refresh:
                existing = await self._fresh_report(db, symbol, "claude", max_age)
                if existing:
                    return self._serialize(existing)
                if await self.llm_backoff_active(db, symbol):
                    return await self._generate(db, symbol, use_llm=False, user_id=user_id)
            result = await self._generate(db, symbol, use_llm=True, user_id=user_id)
            self._llm_recent[symbol] = (time.monotonic(), result)
            return result

    async def _generate(self, db: AsyncSession, symbol: str, use_llm: bool, user_id: Optional[int]) -> Dict[str, Any]:
        dossier = await self.dossier(symbol)
        price = dossier["valuation"].get("price")
        source, model, usage, llm_error = "quant_model", None, None, None
        if use_llm:
            analyst = self.analyst or ClaudeAnalyst()
            try:
                rep = await analyst.write_report(symbol, price, self._tool_handler(dossier))
                usage, model = rep.pop("usage", None), rep.pop("model", None)
                source = "claude"
                self._llm_failed_at.pop(symbol, None)
            except Exception as e:
                llm_error = str(e) or type(e).__name__
                logger.warning("Claude analyst failed for %s (%s); using quant report", symbol, llm_error)
                # Keep what the failed run cost (partial usage rides on the exception).
                usage = {**(getattr(e, "usage", None) or {}), "llm_failed": True,
                         "attempted_model": getattr(e, "model", None) or getattr(analyst, "model", None)}
                self._llm_failed_at[symbol] = time.monotonic()
                rep = quant_report(dossier)
                rep["llm_failed"] = True
                rep["llm_error"] = llm_error
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
                    "altman_model": (f.get("altman") or {}).get("model"),
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
