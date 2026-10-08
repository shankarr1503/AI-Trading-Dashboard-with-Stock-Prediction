"""
AI Trading Platform — FastAPI Application Entry Point

`app` is what uvicorn serves (`uvicorn backend.main:app`). With DESKTOP_MODE
(set by the desktop sidecar, backend/desktop.py) the same app additionally:

* only answers requests whose Host is 127.0.0.1 / localhost (DNS-rebinding defence),
* mounts the desktop control API at /api/desktop/* when TRADEBOT_CONTROL_TOKEN is set,
* serves the static frontend export (TRADEBOT_STATIC_DIR) at "/" on the same origin.

Nothing changes when DESKTOP_MODE is false (credential query parameters are
redacted from uvicorn's logs in every mode: see backend/logging_utils.py).
"""
import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketClose

from backend.alerts.router import router as alerts_router
from backend.auth.router import router as auth_router
from backend.config import settings
from backend.database import models  # noqa: F401  (register tables on Base.metadata)
from backend.database.models import BotPosition, BotState
from backend.database.session import Base, as_utc, engine, get_db
from backend.indicators.router import router as indicators_router
from backend.logging_utils import install_token_redaction
from backend.market_data.router import router as market_router
from backend.market_data.websocket import router as ws_router
from backend.portfolio.router import router as portfolio_router
from backend.predictions.router import router as predictions_router
from backend.ratelimit import limiter
from backend.research.router import router as research_router
from backend.signals.router import router as signals_router
from backend.trading.router import router as bot_router

logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# WebSocket URLs carry the access token (?token=...): keep it out of uvicorn's logs.
install_token_redaction()

DISCLAIMER = (
    "Stock predictions and trading signals are for educational purposes only. "
    "Past performance does not guarantee future results."
)

# Hosts the desktop sidecar answers to. It only listens on 127.0.0.1; checking the
# Host header as well stops a web page from reaching it through DNS rebinding.
DESKTOP_ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# Paths that belong to the API. In desktop mode everything else is the static frontend.
API_PREFIXES = ("/auth", "/api", "/ws", "/health", "/docs", "/redoc", "/openapi.json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI Trading Platform starting (env=%s, trading mode=%s)", settings.APP_ENV, settings.TRADING_MODE)
    if settings.DB_AUTO_CREATE:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ensured (DB_AUTO_CREATE=true)")
    yield
    await engine.dispose()


async def unhandled_exception_handler(request: Request, exc: Exception):
    # Log the details server-side; never leak internals to clients.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@limiter.exempt
async def health_check():
    return {"status": "healthy", "service": "AI Trading Platform", "version": settings.APP_VERSION}


async def root():
    return {
        "message": "AI Trading Platform API",
        "docs": "/docs",
        "health": "/health",
        "version": settings.APP_VERSION,
        "disclaimer": DISCLAIMER,
    }


# ─── Desktop control API (/api/desktop/*) ─────────────────────────────────────

async def require_desktop_token(x_desktop_token: Optional[str] = Header(default=None)) -> None:
    """The Electron shell passes the per-launch token it gave the sidecar; nobody else knows it."""
    expected = settings.TRADEBOT_CONTROL_TOKEN
    supplied = x_desktop_token or ""
    if not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Forbidden")


desktop_router = APIRouter(dependencies=[Depends(require_desktop_token)])


@desktop_router.get("/info")
@limiter.exempt
async def desktop_info(request: Request, db: AsyncSession = Depends(get_db)):
    """What the desktop shell shows in its tray/menu. Read-only: never creates the bot state row."""
    state = await db.get(BotState, 1)
    open_positions = await db.scalar(select(func.count()).select_from(BotPosition))
    last_cycle_at = state.last_cycle_at if state else None
    return {
        "version": getattr(request.app.state, "desktop_version", None) or settings.APP_VERSION,
        "url": getattr(request.app.state, "desktop_url", None) or str(request.base_url).rstrip("/"),
        "data_dir": settings.TRADEBOT_DATA_DIR,
        "trading_mode": settings.TRADING_MODE,
        "bot_enabled": bool(state.enabled) if state else False,
        "halted": bool(state.halted) if state else False,
        "open_positions": int(open_positions or 0),
        "last_cycle_at": as_utc(last_cycle_at).isoformat() if last_cycle_at else None,
    }


@desktop_router.post("/shutdown", status_code=202)
@limiter.exempt
async def desktop_shutdown(request: Request):
    """
    Ask the sidecar to exit. The bot loop stops between cycles (a running cycle
    finishes first), then the HTTP server exits with code 0. The launcher injects
    the stop function as app.state.request_shutdown.
    """
    request_shutdown = getattr(request.app.state, "request_shutdown", None)
    if request_shutdown is None:
        raise HTTPException(status_code=503, detail="Shutdown is not available in this process")
    request_shutdown("control API")
    return {"stopping": True}


# ─── Static frontend (desktop) ────────────────────────────────────────────────

def is_api_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in API_PREFIXES)


class FrontendFiles(StaticFiles):
    """
    The static frontend export, mounted at "/" after every API route, so API routes
    always win. html=True serves index.html for directories (trailingSlash export:
    /dashboard/ → dashboard/index.html, /dashboard → redirect) and 404.html for
    unknown pages. Unknown paths under an API prefix get the API's JSON 404 instead.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":     # unknown WebSocket path: close it like the router would
            await WebSocketClose()(scope, receive, send)
            return
        if scope["type"] == "http" and is_api_path(scope["path"]):
            raise StarletteHTTPException(status_code=404)
        await super().__call__(scope, receive, send)


def _static_dir() -> Optional[str]:
    directory = settings.TRADEBOT_STATIC_DIR
    if not directory:
        return None
    if not os.path.isfile(os.path.join(directory, "index.html")):
        logger.warning("TRADEBOT_STATIC_DIR=%s has no index.html; not serving the frontend", directory)
        return None
    return directory


# ─── App factory ──────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Build the app from the current settings (tests build desktop-mode variants with this)."""
    app = FastAPI(
        title="AI Trading Platform API",
        description=f"""
AI-powered trading analytics and an agentic, risk-managed trading bot.

- Real-time market data and WebSocket streaming
- Technical indicators, regime detection and multi-factor trade signals
- ML ensemble forecasts and news sentiment
- Portfolio analytics
- Trading bot: paper trading by default, cost-aware entries, hard risk limits,
  walk-forward backtesting, optional Claude risk review

> **Disclaimer:** {DISCLAIMER}
""",
        version=settings.APP_VERSION,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    # Desktop launcher hooks (backend/desktop.py fills these in).
    app.state.request_shutdown = None
    app.state.desktop_url = None
    app.state.desktop_version = None

    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    if settings.DESKTOP_MODE:
        # Added last, so it is the outermost middleware and runs before anything else.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=DESKTOP_ALLOWED_HOSTS)

    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
    app.include_router(market_router, prefix="/api/market", tags=["Market Data"])
    app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])
    app.include_router(indicators_router, prefix="/api/indicators", tags=["Technical Indicators"])
    app.include_router(predictions_router, prefix="/api/predict", tags=["AI Predictions"])
    app.include_router(signals_router, prefix="/api/signals", tags=["Trade Signals"])
    app.include_router(portfolio_router, prefix="/api/portfolio", tags=["Portfolio"])
    app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])
    app.include_router(bot_router, prefix="/api/bot", tags=["Trading Bot"])
    app.include_router(research_router, prefix="/api/research", tags=["Equity Research"])
    if settings.DESKTOP_MODE and settings.TRADEBOT_CONTROL_TOKEN:
        app.include_router(desktop_router, prefix="/api/desktop", tags=["Desktop"])

    app.add_api_route("/health", health_check, methods=["GET"], tags=["Health"])

    static_dir = _static_dir() if settings.DESKTOP_MODE else None
    if static_dir:
        # Must be the last route: the mount at "/" matches every path.
        app.mount("/", FrontendFiles(directory=static_dir, html=True), name="frontend")
    else:
        app.add_api_route("/", root, methods=["GET"], tags=["Root"])
    return app


app = create_app()
