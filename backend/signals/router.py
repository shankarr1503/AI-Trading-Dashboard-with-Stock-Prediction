"""Signals router."""
from fastapi import APIRouter, HTTPException
from backend.signals.service import signal_service

router = APIRouter()


@router.get("/{symbol}")
async def get_signal(symbol: str):
    """
    Get AI-driven trade signal for a stock symbol.

    Aggregates:
    - Technical indicator signals (RSI, MACD, Bollinger, EMA, Stochastic)
    - ML price prediction (direction + confidence)
    - Market sentiment (FinBERT NLP score)

    Returns BUY / SELL / HOLD with entry price, target, stop-loss, and confidence.

    ⚠️ DISCLAIMER: For educational analysis only. Not financial advice.
    """
    try:
        return await signal_service.generate_signal(symbol.upper())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Signal generation failed: {e}")
