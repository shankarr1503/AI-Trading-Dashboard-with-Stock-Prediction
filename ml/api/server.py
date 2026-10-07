"""
ML Service — internal microservice for return forecasts and news sentiment.
Consumed by the backend; not meant to be exposed publicly (no CORS).

Endpoints are plain `def` so FastAPI runs the CPU/IO-bound model work in its
threadpool instead of blocking the event loop.
"""
import hmac
import logging
import os
import re

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from ml.ensemble.predictor import registry
from ml.sentiment.analyzer import sentiment_analyzer

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("ML_SERVICE_API_KEY", "")
_SYMBOL_RE = re.compile(r"^[A-Z0-9^.\-=]{1,20}$")

app = FastAPI(
    title="AI Trading — ML Service",
    description="Skill-weighted LSTM + XGBoost + ARIMA return forecasts and FinBERT news sentiment.",
    version="2.0.0",
)


def _symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not _SYMBOL_RE.match(s):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    return s


def _require_key(x_api_key: str) -> None:
    if not API_KEY:
        raise HTTPException(status_code=403, detail="Training is disabled: set ML_SERVICE_API_KEY")
    if not hmac.compare_digest(x_api_key or "", API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health():
    return {"status": "healthy", "service": "ml_service"}


@app.get("/predict/{symbol}")
def predict(symbol: str):
    """Ensemble next-day / next-week forecast. Uses trained models when present, ARIMA otherwise."""
    sym = _symbol(symbol)
    try:
        return registry.get(sym).predict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Prediction failed for %s", sym)
        raise HTTPException(status_code=500, detail="Prediction failed")


@app.get("/sentiment/{symbol}")
def get_sentiment(symbol: str):
    sym = _symbol(symbol)
    try:
        return sentiment_analyzer.fetch_and_analyze(sym)
    except Exception:
        logger.exception("Sentiment analysis failed for %s", sym)
        raise HTTPException(status_code=500, detail="Sentiment analysis failed")


def _train(sym: str, period: str) -> None:
    try:
        report = registry.get(sym).train_all(period)
        registry.reload(sym)
        logger.info("Training finished for %s: %s", sym, report)
    except Exception:
        logger.exception("Training failed for %s", sym)


@app.post("/train/{symbol}", status_code=202)
def train_model(symbol: str, background: BackgroundTasks, period: str = "5y", x_api_key: str = Header(default="")):
    """Start (re)training for a symbol in the background. Requires the X-API-Key header."""
    _require_key(x_api_key)
    sym = _symbol(symbol)
    if period not in ("2y", "5y", "10y", "max"):
        raise HTTPException(status_code=422, detail="period must be one of 2y, 5y, 10y, max")
    background.add_task(_train, sym, period)
    return {"message": f"Training started for {sym}", "period": period}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("ml.api.server:app", host="0.0.0.0", port=int(os.environ.get("ML_SERVICE_PORT", 8001)))
