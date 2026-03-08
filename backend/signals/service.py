"""
AI Trade Signal Engine.
Combines technical indicator signals + ML prediction + sentiment → BUY/SELL/HOLD.
"""
import logging
from typing import Dict, Any
import httpx

from backend.predictions.service import prediction_service
from backend.indicators.service import indicator_service
from backend.market_data.service import market_data_service
from backend.config import settings

logger = logging.getLogger(__name__)


class SignalService:
    """Generates AI-driven BUY/SELL/HOLD signals with entry/target/stop-loss."""

    async def generate_signal(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()

        # Gather inputs concurrently (simplified sequential for clarity)
        quote = await market_data_service.get_quote(symbol)
        current_price = quote.get("current_price", 0)

        # Get technical indicator signals
        try:
            indicators = await indicator_service.compute_all(symbol, period="3mo", interval="1d")
            ind_signals = indicators.get("signals", {})
        except Exception as e:
            logger.warning(f"Indicators failed: {e}")
            ind_signals = {}

        # Get ML prediction
        try:
            prediction = await prediction_service.predict(symbol)
            pred_direction = prediction.get("predictions", {}).get("next_day", {}).get("direction", "NEUTRAL")
            pred_confidence = prediction.get("predictions", {}).get("next_day", {}).get("confidence", 0.5)
        except Exception as e:
            logger.warning(f"Prediction failed: {e}")
            pred_direction = "NEUTRAL"
            pred_confidence = 0.5

        # Get market sentiment (stub — real FinBERT called via ML service)
        sentiment = await self._get_sentiment(symbol)
        sentiment_score = sentiment.get("compound_score", 0.0)  # -1 to 1

        # ── Aggregate Signals ──────────────────────────────────────────────────
        buy_votes = 0
        sell_votes = 0
        hold_votes = 0

        for sig in ind_signals.values():
            s = sig.get("signal", "NEUTRAL")
            if s == "BUY":
                buy_votes += 1
            elif s == "SELL":
                sell_votes += 1
            else:
                hold_votes += 1

        # ML prediction vote (weighted 2x)
        if pred_direction == "BULLISH":
            buy_votes += 2
        elif pred_direction == "BEARISH":
            sell_votes += 2
        else:
            hold_votes += 2

        # Sentiment vote
        if sentiment_score > 0.15:
            buy_votes += 1
        elif sentiment_score < -0.15:
            sell_votes += 1
        else:
            hold_votes += 1

        total_votes = buy_votes + sell_votes + hold_votes

        if buy_votes > sell_votes and buy_votes > hold_votes:
            signal_type = "BUY"
            confidence = buy_votes / total_votes
        elif sell_votes > buy_votes and sell_votes > hold_votes:
            signal_type = "SELL"
            confidence = sell_votes / total_votes
        else:
            signal_type = "HOLD"
            confidence = max(hold_votes, buy_votes, sell_votes) / total_votes

        # ── Price Targets ──────────────────────────────────────────────────────
        atr_val = indicators.get("current", {}).get("ATR_14") if "indicators" in dir() else None
        volatility_factor = (atr_val / current_price) if atr_val and current_price else 0.02

        if signal_type == "BUY":
            entry_price = current_price
            target_price = round(current_price * (1 + max(volatility_factor * 2, 0.03)), 4)
            stop_loss = round(current_price * (1 - max(volatility_factor, 0.015)), 4)
        elif signal_type == "SELL":
            entry_price = current_price
            target_price = round(current_price * (1 - max(volatility_factor * 2, 0.03)), 4)
            stop_loss = round(current_price * (1 + max(volatility_factor, 0.015)), 4)
        else:
            entry_price = current_price
            target_price = current_price
            stop_loss = round(current_price * 0.98, 4)

        risk = abs(entry_price - stop_loss)
        reward = abs(target_price - entry_price)
        risk_reward = round(reward / risk, 2) if risk > 0 else 0

        return {
            "symbol": symbol,
            "signal": signal_type,
            "confidence": round(confidence, 3),
            "confidence_pct": round(confidence * 100, 1),
            "current_price": current_price,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "risk_reward_ratio": risk_reward,
            "votes": {
                "buy": buy_votes,
                "sell": sell_votes,
                "hold": hold_votes,
            },
            "components": {
                "technical_signals": ind_signals,
                "ml_prediction": {
                    "direction": pred_direction,
                    "confidence": pred_confidence,
                },
                "sentiment": sentiment,
            },
            "disclaimer": (
                "⚠️ AI signals are for educational analysis only. "
                "Always conduct your own research before making investment decisions."
            ),
        }

    async def _get_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Call ML service for FinBERT sentiment, with neutral fallback."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{settings.ML_SERVICE_URL}/sentiment/{symbol}")
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass

        return {
            "symbol": symbol,
            "positive": 0.35,
            "negative": 0.25,
            "neutral": 0.40,
            "overall": "NEUTRAL",
            "compound_score": 0.05,
            "source": "fallback",
        }


signal_service = SignalService()
