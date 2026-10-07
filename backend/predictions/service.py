"""
Prediction + sentiment service.

Calls the ML microservice and falls back to a transparent statistical model when
it is unavailable. Results are cached so the dashboard and the signal engine
don't trigger duplicate model runs for the same symbol.
"""
import logging
import math
from typing import Any, Dict

import httpx
import numpy as np

from backend.cache import cache_get, cache_set
from backend.config import settings
from backend.market_data.service import market_data_service, validate_symbol
from shared import sentiment_lexicon

logger = logging.getLogger(__name__)

PREDICTION_TTL = 300
SENTIMENT_TTL = 900

DISCLAIMER = (
    "Predictions are for educational purposes only. Markets are inherently uncertain; "
    "do not base financial decisions solely on these outputs."
)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def statistical_forecast(closes: list[float]) -> Dict[str, Any]:
    """
    Drift + volatility model on log returns.

    Expected daily return = exponentially weighted mean of recent log returns
    (shrunk toward zero, since short-horizon drift estimates are mostly noise);
    P(up) = Φ(μ/σ), i.e. the probability of a positive return if returns are
    roughly normal with that mean and volatility. This yields honest
    probabilities close to 50% instead of presenting a heuristic "confidence"
    as a probability.
    """
    arr = np.asarray(closes, dtype=float)
    log_ret = np.diff(np.log(arr))
    if len(log_ret) < 20:
        raise ValueError("Insufficient historical data")
    recent = log_ret[-60:]
    weights = np.exp(np.linspace(-1.5, 0, len(recent)))
    mu_raw = float(np.sum(weights * recent) / np.sum(weights))
    sigma = float(np.std(recent, ddof=1)) or 1e-6
    shrink = 0.5
    mu = mu_raw * shrink

    current = float(arr[-1])
    p_up_1d = normal_cdf(mu / sigma)
    p_up_5d = normal_cdf((mu * 5) / (sigma * math.sqrt(5)))
    next_day = current * math.exp(mu)
    next_week = current * math.exp(mu * 5)
    return {
        "current_price": current,
        "mu": mu,
        "sigma": sigma,
        "p_up_1d": p_up_1d,
        "p_up_5d": p_up_5d,
        "next_day": next_day,
        "next_week": next_week,
    }


class PredictionService:
    async def predict(self, symbol: str) -> Dict[str, Any]:
        symbol = validate_symbol(symbol)
        key = f"prediction:{symbol}"
        cached = await cache_get(key)
        if cached is not None:
            return cached

        result = None
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(f"{settings.ML_SERVICE_URL}/predict/{symbol}")
            if resp.status_code == 200:
                result = resp.json()
                result["source"] = "ml_service"
            else:
                logger.info("ML service returned %s for %s", resp.status_code, symbol)
        except httpx.HTTPError as e:
            logger.info("ML service unavailable (%s); using statistical fallback", e)

        if result is None:
            result = await self._statistical_prediction(symbol)
        await cache_set(key, result, PREDICTION_TTL)
        return result

    async def _statistical_prediction(self, symbol: str) -> Dict[str, Any]:
        history = await market_data_service.get_history(symbol, period="6mo", interval="1d")
        closes = [h["close"] for h in history if h.get("close")]
        f = statistical_forecast(closes)
        current = f["current_price"]
        change_pct = (f["next_day"] / current - 1) * 100
        direction = "BULLISH" if change_pct > 0 else "BEARISH" if change_pct < 0 else "NEUTRAL"
        # Confidence = how far the probability is from a coin flip (0..1).
        confidence = abs(f["p_up_1d"] - 0.5) * 2
        return {
            "symbol": symbol,
            "current_price": round(current, 4),
            "predictions": {
                "next_day": {
                    "price": round(f["next_day"], 4),
                    "change": round(f["next_day"] - current, 4),
                    "change_pct": round(change_pct, 4),
                    "direction": direction,
                    "confidence": round(confidence, 3),
                    "bullish_probability": round(f["p_up_1d"], 3),
                    "bearish_probability": round(1 - f["p_up_1d"], 3),
                    "expected_volatility_pct": round(f["sigma"] * 100, 3),
                },
                "next_week": {
                    "price": round(f["next_week"], 4),
                    "change_pct": round((f["next_week"] / current - 1) * 100, 4),
                    "direction": "BULLISH" if f["next_week"] > current else "BEARISH",
                    "bullish_probability": round(f["p_up_5d"], 3),
                },
            },
            "model_breakdown": {
                "DriftVolatility": {
                    "predicted_price": round(f["next_day"], 4),
                    "change_pct": round(change_pct, 4),
                    "direction": direction,
                    "weight": 1.0,
                },
            },
            "ensemble_method": "drift_volatility",
            "source": "statistical_fallback",
            "disclaimer": DISCLAIMER,
        }

    async def sentiment(self, symbol: str) -> Dict[str, Any]:
        """FinBERT sentiment via the ML service, else lexicon scoring of real headlines."""
        symbol = validate_symbol(symbol)
        key = f"sentiment:{symbol}"
        cached = await cache_get(key)
        if cached is not None:
            return cached

        result = None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{settings.ML_SERVICE_URL}/sentiment/{symbol}")
            if resp.status_code == 200:
                result = resp.json()
        except httpx.HTTPError:
            pass

        if result is None:
            headlines = await market_data_service.get_news(symbol)
            agg = sentiment_lexicon.aggregate([sentiment_lexicon.score_text(h) for h in headlines])
            result = {
                "symbol": symbol,
                **agg,
                "headlines": headlines[:5],
                "headlines_analyzed": len(headlines),
                "source": "lexicon" if headlines else "unavailable",
            }
        await cache_set(key, result, SENTIMENT_TTL)
        return result


prediction_service = PredictionService()
