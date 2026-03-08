"""
Ensemble Predictor — aggregates LSTM, XGBoost, and ARIMA predictions
into a single consensus forecast with weighted confidence scoring.
"""
import logging
import os
import numpy as np
import pandas as pd
import yfinance as yf
from typing import Dict, Any, Optional

from ml.feature_engineering.pipeline import build_features
from ml.models.lstm_model import LSTMStockPredictor
from ml.models.xgboost_model import XGBoostPredictor
from ml.models.arima_model import ARIMAPredictor
from ml.evaluation.metrics import compute_metrics

logger = logging.getLogger(__name__)

# Model weights for ensemble (higher = more influence)
MODEL_WEIGHTS = {
    "LSTM": 0.40,
    "XGBoost": 0.35,
    "ARIMA": 0.25,
}

# Feature columns used by tabular models (LSTM + XGBoost)
TABULAR_FEATURES = [
    "close", "return_1d", "return_5d", "return_10d", "log_return_1d",
    "sma_10", "sma_20", "ema_10", "ema_20", "rsi_14", "rsi_7",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_lower", "bb_bandwidth", "bb_pct",
    "atr_14", "volatility_10d", "volatility_20d",
    "volume_ratio", "daily_range_pct",
    "stoch_k", "stoch_d", "willr",
]


class EnsemblePredictor:
    """Weighted ensemble of LSTM + XGBoost + ARIMA for stock price prediction."""

    def __init__(self, model_dir: str = "ml/saved_models"):
        self.model_dir = model_dir
        from ml.models.lstm_model import TF_AVAILABLE
        self.lstm = LSTMStockPredictor(sequence_length=60) if TF_AVAILABLE else None
        self.xgb = XGBoostPredictor()
        self.arima = ARIMAPredictor()
        self.models_loaded = False

    def _fetch_data(self, symbol: str, period: str = "2y") -> pd.DataFrame:
        """Fetch OHLCV from Yahoo Finance and compute features."""
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval="1d", auto_adjust=True)
        df.columns = df.columns.str.lower()
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df = build_features(df)
        return df

    def train_all(self, symbol: str):
        """Train all three models on historical data for the given symbol."""
        logger.info(f"Starting ensemble training for {symbol}...")
        df = self._fetch_data(symbol)

        # Filter available feature columns
        available_features = [f for f in TABULAR_FEATURES if f in df.columns]

        # Train LSTM
        if self.lstm:
            try:
                logger.info("Training LSTM...")
                self.lstm.train(df, available_features, epochs=50, batch_size=32)
                self.lstm.save(os.path.join(self.model_dir, symbol, "lstm"))
            except Exception as e:
                logger.error(f"LSTM training failed: {e}")
        else:
            logger.warning("Skipping LSTM training (TensorFlow missing)")

        # Train XGBoost
        try:
            logger.info("Training XGBoost...")
            self.xgb.train(df, available_features)
            self.xgb.save(os.path.join(self.model_dir, symbol, "xgb"))
        except Exception as e:
            logger.error(f"XGBoost training failed: {e}")

        # Train ARIMA
        try:
            logger.info("Training ARIMA...")
            self.arima.train(df)
            self.arima.save(os.path.join(self.model_dir, symbol, "arima"))
        except Exception as e:
            logger.error(f"ARIMA training failed: {e}")

        logger.info(f"Ensemble training complete for {symbol}")

    def load_all(self, symbol: str) -> bool:
        """Load pre-trained models from disk."""
        base = os.path.join(self.model_dir, symbol)
        loaded = False
        if self.lstm:
            try:
                self.lstm.load(os.path.join(base, "lstm"))
                loaded = True
            except Exception:
                pass
        try:
            self.xgb.load(os.path.join(base, "xgb"))
            loaded = True
        except Exception:
            pass
        try:
            self.arima.load(os.path.join(base, "arima"))
            loaded = True
        except Exception:
            pass
        self.models_loaded = loaded
        return loaded

    def predict(self, symbol: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Generate ensemble prediction for a symbol.
        Auto-loads trained models if available, otherwise falls back to ARIMA only.
        """
        symbol = symbol.upper()

        if use_cache:
            self.load_all(symbol)

        df = self._fetch_data(symbol)
        available_features = [f for f in TABULAR_FEATURES if f in df.columns]
        current_price = float(df["close"].iloc[-1])

        model_preds = {}

        # LSTM prediction
        if self.lstm and self.lstm.is_trained:
            try:
                model_preds["LSTM"] = self.lstm.predict(df)
            except Exception as e:
                logger.warning(f"LSTM predict failed: {e}")

        # XGBoost prediction
        if self.xgb.is_trained:
            try:
                model_preds["XGBoost"] = self.xgb.predict(df)
            except Exception as e:
                logger.warning(f"XGBoost predict failed: {e}")

        # ARIMA prediction (always attempt)
        try:
            model_preds["ARIMA"] = self.arima.predict(df)
        except Exception as e:
            logger.warning(f"ARIMA predict failed: {e}")

        if not model_preds:
            raise RuntimeError("All models failed to predict.")

        # ── Weighted Ensemble ──────────────────────────────────────────────────
        total_weight = 0
        weighted_sum = 0
        for model_name, pred in model_preds.items():
            w = MODEL_WEIGHTS.get(model_name, 0.33)
            weighted_sum += pred["predicted_price"] * w
            total_weight += w

        ensemble_price = weighted_sum / total_weight if total_weight > 0 else current_price
        change_pct = (ensemble_price - current_price) / current_price * 100
        direction = "BULLISH" if change_pct > 0 else "BEARISH"

        # Confidence: inversely proportional to model disagreement
        pred_prices = [p["predicted_price"] for p in model_preds.values()]
        std_of_preds = float(np.std(pred_prices)) if len(pred_prices) > 1 else 0
        disagreement_pct = (std_of_preds / current_price) * 100
        confidence = max(0.35, min(0.90, 1.0 - disagreement_pct / 5))

        bullish_prob = confidence if direction == "BULLISH" else round(1 - confidence, 3)
        bearish_prob = confidence if direction == "BEARISH" else round(1 - confidence, 3)

        return {
            "symbol": symbol,
            "current_price": round(current_price, 4),
            "predictions": {
                "next_day": {
                    "price": round(ensemble_price, 4),
                    "change": round(ensemble_price - current_price, 4),
                    "change_pct": round(change_pct, 4),
                    "direction": direction,
                    "confidence": round(confidence, 3),
                    "bullish_probability": round(bullish_prob, 3),
                    "bearish_probability": round(bearish_prob, 3),
                },
            },
            "model_breakdown": {
                name: {
                    "predicted_price": pred["predicted_price"],
                    "change_pct": pred["change_pct"],
                    "direction": pred["direction"],
                    "weight": MODEL_WEIGHTS.get(name, 0.33),
                }
                for name, pred in model_preds.items()
            },
            "ensemble_method": "weighted_average",
            "models_used": list(model_preds.keys()),
            "disclaimer": (
                "⚠️ AI predictions are for educational analysis only. "
                "Stock markets are inherently unpredictable. "
                "Do NOT base financial decisions solely on these outputs."
            ),
        }


# Singleton
ensemble_predictor = EnsemblePredictor()
