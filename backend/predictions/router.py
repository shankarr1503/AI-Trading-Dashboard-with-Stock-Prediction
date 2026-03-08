"""Predictions router."""
from fastapi import APIRouter, HTTPException
from backend.predictions.service import prediction_service

router = APIRouter()


@router.get("/{symbol}")
async def get_prediction(symbol: str):
    """
    Get AI-powered stock price prediction for a symbol.

    Returns:
    - Next-day price prediction
    - Next-week trend prediction
    - Bullish/Bearish probability
    - Confidence score
    - Model breakdown

    ⚠️ DISCLAIMER: For educational purposes only. Not financial advice.
    """
    try:
        return await prediction_service.predict(symbol.upper())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")
