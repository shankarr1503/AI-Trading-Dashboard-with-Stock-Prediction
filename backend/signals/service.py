"""
Trade signal engine — the same analysis the trading bot uses, exposed for humans.

Combines regime-weighted technical factors, the ML forecast and news sentiment
into a BUY / SELL / HOLD call with ATR-based stop and target, an estimate of
the trade's expectancy, and whether that expectancy clears transaction costs.
"""
import asyncio
import logging
from typing import Any, Dict

from backend.indicators.service import compute_indicator_signals
from backend.market_data.service import market_data_service, validate_symbol
from backend.predictions.service import prediction_service
from backend.trading.calibration import Calibrator
from backend.trading.costs import cost_model_for
from backend.trading.strategy import StrategyConfig, analyze_latest, compute_factor_frame

logger = logging.getLogger(__name__)


async def _optional(coro, timeout: float = 30.0):
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception as e:
        logger.info("Optional signal input unavailable: %s", e)
        return None


class SignalService:
    async def generate_signal(self, symbol: str) -> Dict[str, Any]:
        symbol = validate_symbol(symbol)
        cfg = StrategyConfig()
        df = await market_data_service.get_history_df(symbol, period="2y", interval="1d")
        frame = await asyncio.to_thread(compute_factor_frame, df, cfg)
        prediction, sentiment = await asyncio.gather(
            _optional(prediction_service.predict(symbol)),
            _optional(prediction_service.sentiment(symbol)),
        )
        a = analyze_latest(symbol, frame, cfg, ml=prediction, sentiment=sentiment)

        from backend.database.session import AsyncSessionLocal
        from backend.trading.agent import load_calibrator

        try:
            async with AsyncSessionLocal() as db:
                calibrator = await load_calibrator(db, symbol)
        except Exception:
            calibrator = Calibrator()
        edge = calibrator.estimate(a.score)
        cm = cost_model_for(symbol)
        risk_pct = (a.price - a.stop) / a.price
        expected_edge_pct = edge.ev_r * risk_pct
        cost_pct = cm.round_trip_cost_pct(a.price)
        worth_it = a.signal == "BUY" and expected_edge_pct >= 2 * cost_pct and edge.ev_r > 0

        current = {k: (None if v != v else v) for k, v in frame.iloc[-1].items() if isinstance(v, (int, float))}
        nd = ((prediction or {}).get("predictions") or {}).get("next_day") or {}
        return {
            "symbol": symbol,
            "signal": a.signal,
            "score": a.score,
            "confidence": round(abs(a.score), 3),
            "confidence_pct": round(abs(a.score) * 100, 1),
            "regime": a.regime,
            "current_price": a.price,
            "entry_price": a.price,
            "target_price": a.target if a.signal == "BUY" else None,
            "stop_loss": a.stop if a.signal == "BUY" else None,
            "risk_reward_ratio": a.reward_risk if a.signal == "BUY" else None,
            "edge": edge.to_dict(),
            "expected_edge_pct": round(expected_edge_pct * 100, 3),
            "round_trip_cost_pct": round(cost_pct * 100, 3),
            "worth_the_costs": worth_it,
            "reasons": a.reasons,
            "components": {
                "factors": a.components,
                "technical_signals": compute_indicator_signals(current),
                "ml_prediction": {
                    "direction": nd.get("direction", "NEUTRAL"),
                    "bullish_probability": nd.get("bullish_probability"),
                    "source": (prediction or {}).get("source"),
                },
                "sentiment": sentiment,
            },
            "disclaimer": "Signals are for educational analysis only and are not financial advice.",
        }


signal_service = SignalService()
