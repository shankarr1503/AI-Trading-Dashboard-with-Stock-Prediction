"""
AI Trading Platform — FastAPI Application Entry Point
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from backend.alerts.router import router as alerts_router
from backend.auth.router import router as auth_router
from backend.config import settings
from backend.database import models  # noqa: F401  (register tables on Base.metadata)
from backend.database.session import Base, engine
from backend.indicators.router import router as indicators_router
from backend.market_data.router import router as market_router
from backend.market_data.websocket import router as ws_router
from backend.portfolio.router import router as portfolio_router
from backend.predictions.router import router as predictions_router
from backend.ratelimit import limiter
from backend.signals.router import router as signals_router
from backend.trading.router import router as bot_router

logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Stock predictions and trading signals are for educational purposes only. "
    "Past performance does not guarantee future results."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI Trading Platform starting (env=%s, trading mode=%s)", settings.APP_ENV, settings.TRADING_MODE)
    if settings.DB_AUTO_CREATE:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ensured (DB_AUTO_CREATE=true)")
    yield
    await engine.dispose()


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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Log the details server-side; never leak internals to clients.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(market_router, prefix="/api/market", tags=["Market Data"])
app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])
app.include_router(indicators_router, prefix="/api/indicators", tags=["Technical Indicators"])
app.include_router(predictions_router, prefix="/api/predict", tags=["AI Predictions"])
app.include_router(signals_router, prefix="/api/signals", tags=["Trade Signals"])
app.include_router(portfolio_router, prefix="/api/portfolio", tags=["Portfolio"])
app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(bot_router, prefix="/api/bot", tags=["Trading Bot"])


@app.get("/health", tags=["Health"])
@limiter.exempt
async def health_check():
    return {"status": "healthy", "service": "AI Trading Platform", "version": settings.APP_VERSION}


@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "AI Trading Platform API",
        "docs": "/docs",
        "health": "/health",
        "version": settings.APP_VERSION,
        "disclaimer": DISCLAIMER,
    }
