"""
ML Prediction Service — calls ML microservice and returns price forecasts.
Falls back to a simple statistical heuristic if ML service is unavailable.
"""
import logging
import math
import statistics
from typing import Dict, Any
import httpx

from backend.config import settings
from backend.market_data.service import market_data_service

logger = logging.getLogger(__name__)


class PredictionService:
    """Calls the ML service for stock price predictions with statistical fallback."""

    async def predict(self, symbol: str) -> Dict[str, Any]:
        """Get ensemble price prediction from the ML service."""
        symbol = symbol.upper()

        # Try calling the dedicated ML microservice first
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{settings.ML_SERVICE_URL}/predict/{symbol}")
                if resp.status_code == 200:
                    data = resp.json()
                    data["source"] = "ml_service"
                    return data
        except Exception as e:
            logger.warning(f"ML service unavailable ({e}), using statistical fallback")

        # Statistical fallback: use historical data to compute a simple prediction
        return await self._statistical_prediction(symbol)

    async def _statistical_prediction(self, symbol: str) -> Dict[str, Any]:
        """
        Fallback prediction using statistical analysis of recent price movements.
        Computes a next-day price forecast using linear regression on recent closes.
        """
        try:
            history = await market_data_service.get_history(symbol, period="3mo", interval="1d")
            closes = [h["close"] for h in history if h.get("close")]
            if len(closes) < 20:
                raise ValueError("Insufficient historical data")

            # Linear regression slope (simple trend)
            n = len(closes)
            x = list(range(n))
            mean_x = sum(x) / n
            mean_y = sum(closes) / n
            slope = sum((x[i] - mean_x) * (closes[i] - mean_y) for i in range(n)) / \
                    sum((x[i] - mean_x) ** 2 for i in range(n))

            current_price = closes[-1]
            predicted_next_day = current_price + slope

            # Volatility-based confidence
            std = statistics.stdev(closes[-20:])
            volatility = std / current_price
            confidence = max(0.30, min(0.85, 1.0 - (volatility * 10)))

            price_change_pct = ((predicted_next_day - current_price) / current_price) * 100
            direction = "BULLISH" if price_change_pct > 0 else "BEARISH"

            # Week prediction (7 steps)
            predicted_week = current_price + slope * 7

            # Models breakdown
            models = {
                "LinearRegression": {
                    "predicted_price": round(predicted_next_day, 4),
                    "confidence": round(confidence, 3),
                },
                "Statistical": {
                    "predicted_price": round(predicted_next_day, 4),
                    "confidence": round(confidence * 0.85, 3),
                },
            }

            return {
                "symbol": symbol,
                "current_price": round(current_price, 4),
                "predictions": {
                    "next_day": {
                        "price": round(predicted_next_day, 4),
                        "change": round(predicted_next_day - current_price, 4),
                        "change_pct": round(price_change_pct, 4),
                        "direction": direction,
                        "confidence": round(confidence, 3),
                        "bullish_probability": round(confidence if direction == "BULLISH" else 1 - confidence, 3),
                        "bearish_probability": round(confidence if direction == "BEARISH" else 1 - confidence, 3),
                    },
                    "next_week": {
                        "price": round(predicted_week, 4),
                        "change_pct": round(((predicted_week - current_price) / current_price) * 100, 4),
                        "direction": "BULLISH" if predicted_week > current_price else "BEARISH",
                        "confidence": round(confidence * 0.70, 3),
                    },
                },
                "model_breakdown": models,
                "ensemble_method": "statistical_regression",
                "source": "statistical_fallback",
                "disclaimer": (
                    "⚠️ This prediction is for educational purposes only. "
                    "Stock market predictions are inherently uncertain and should not "
                    "be used as the sole basis for financial decisions."
                ),
            }

        except Exception as e:
            logger.error(f"Statistical prediction failed for {symbol}: {e}")
            raise


prediction_service = PredictionService()
