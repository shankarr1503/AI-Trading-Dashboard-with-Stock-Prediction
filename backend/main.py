"""
AI Trading Platform — FastAPI Application Entry Point
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from backend.config import settings
from backend.database.session import engine, Base
from backend.auth.router import router as auth_router
from backend.market_data.router import router as market_router
from backend.market_data.websocket import router as ws_router
from backend.indicators.router import router as indicators_router
from backend.predictions.router import router as predictions_router
from backend.signals.router import router as signals_router
from backend.portfolio.router import router as portfolio_router
from backend.alerts.router import router as alerts_router

logger = logging.getLogger(__name__)

# ─── Rate Limiter ───────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])


# ─── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("🚀 AI Trading Platform starting up...")
    try:
        # Create tables (handle SQLite/Postgres gracefully)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Database tables initialized")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        logger.warning("Proceeding without database tables. CRUD operations will fail.")
    yield
    logger.info("🛑 AI Trading Platform shutting down...")


# ─── App Instance ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="AI Trading Platform API",
    description="""
    ## 🚀 AI-Powered Trading Analytics Platform

    A production-grade trading API providing:
    - **Real-time market data** (OHLCV, bid/ask, market depth)
    - **Technical indicators** (SMA, EMA, RSI, MACD, Bollinger Bands, etc.)
    - **AI stock predictions** (LSTM, XGBoost, ARIMA ensemble)
    - **Market sentiment analysis** (FinBERT NLP)
    - **AI trade signals** (BUY/SELL/HOLD with confidence)
    - **Portfolio analytics** (P&L, Sharpe, drawdown)
    - **Smart alerts** (price, volume, signal triggers)

    > ⚠️ **DISCLAIMER**: Stock predictions are for educational purposes only. 
    > Past performance does not guarantee future results.
    """,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ─── Middleware ───────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── Routers ─────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(market_router, prefix="/api/market", tags=["Market Data"])
app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])
app.include_router(indicators_router, prefix="/api/indicators", tags=["Technical Indicators"])
app.include_router(predictions_router, prefix="/api/predict", tags=["AI Predictions"])
app.include_router(signals_router, prefix="/api/signals", tags=["Trade Signals"])
app.include_router(portfolio_router, prefix="/api/portfolio", tags=["Portfolio"])
app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health_check():
    """Platform health check endpoint."""
    return JSONResponse({
        "status": "healthy",
        "service": "AI Trading Platform",
        "version": settings.APP_VERSION,
        "disclaimer": (
            "Stock predictions are for educational purposes only. "
            "Past performance does not guarantee future results."
        ),
    })


@app.get("/", tags=["Root"])
async def root():
    """API root — redirect to docs."""
    return {
        "message": "Welcome to AI Trading Platform API",
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
        "version": settings.APP_VERSION,
    }
