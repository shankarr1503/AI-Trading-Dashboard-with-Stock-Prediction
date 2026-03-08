"""
ML Service FastAPI Server — standalone microservice for stock predictions and sentiment.
Consulted by the backend predictions and signals services.
"""
import os
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ml.ensemble.predictor import ensemble_predictor
from ml.sentiment.analyzer import sentiment_analyzer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Trading — ML Prediction Service",
    description="Standalone ML microservice: LSTM + XGBoost + ARIMA ensemble + FinBERT sentiment",
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "healthy", "service": "ml_prediction_service"}


@app.get("/predict/{symbol}")
async def predict(symbol: str, train: bool = False):
    """
    Generate ensemble stock price prediction.
    - Set ?train=true to retrain models (slow, requires TF + XGBoost installed)
    - Default: loads pre-trained models or falls back to ARIMA-only
    """
    try:
        if train:
            ensemble_predictor.train_all(symbol.upper())
        return ensemble_predictor.predict(symbol.upper())
    except Exception as e:
        logger.error(f"Prediction failed for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@app.get("/sentiment/{symbol}")
async def get_sentiment(symbol: str):
    """Run FinBERT sentiment analysis on recent financial news for a symbol."""
    try:
        return await sentiment_analyzer.fetch_and_analyze(symbol.upper())
    except Exception as e:
        logger.error(f"Sentiment analysis failed for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=f"Sentiment analysis failed: {e}")


@app.get("/train/{symbol}")
async def train_model(symbol: str):
    """
    Trigger model training for a symbol.
    This is a long-running operation. In production, submit to a task queue.
    """
    try:
        ensemble_predictor.train_all(symbol.upper())
        return {"message": f"Models trained successfully for {symbol.upper()}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Training failed: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ML_SERVICE_PORT", 8001))
    uvicorn.run("ml.api.server:app", host="0.0.0.0", port=port, reload=False)
