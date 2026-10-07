"""Predictions router."""
import logging

from fastapi import APIRouter, HTTPException

from backend.predictions.service import prediction_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/{symbol}")
async def get_prediction(symbol: str):
    """
    Next-day / next-week price forecast with model-implied up/down probabilities.

    DISCLAIMER: For educational purposes only. Not financial advice.
    """
    try:
        return await prediction_service.predict(symbol)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Prediction failed for %s", symbol)
        raise HTTPException(status_code=500, detail="Prediction failed")


@router.get("/{symbol}/sentiment")
async def get_sentiment(symbol: str):
    """News sentiment for a symbol (FinBERT via the ML service, lexicon fallback)."""
    try:
        return await prediction_service.sentiment(symbol)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
