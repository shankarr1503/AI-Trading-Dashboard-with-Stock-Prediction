"""
Ensemble predictor — combines LSTM, XGBoost and ARIMA next-day return forecasts.

Each symbol gets its own predictor instance (previously a single shared
instance meant symbol B could be scored with symbol A's models). Models are
weighted by out-of-sample skill (holdout RMSE relative to a naive zero-return
forecast). The up/down probability is P(r > 0) under a normal approximation
with the ensemble mean and recent realised volatility — which is usually close
to 50%, because next-day returns are mostly noise. That is the honest answer.
"""
import logging
import math
import os
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from ml.feature_engineering.pipeline import FEATURE_COLUMNS, build_features
from ml.models.arima_model import ARIMAPredictor
from ml.models.lstm_model import TF_AVAILABLE, LSTMStockPredictor
from ml.models.xgboost_model import XGBoostPredictor

logger = logging.getLogger(__name__)

MODEL_DIR = os.environ.get("MODEL_SAVE_DIR", "ml/saved_models")
DISCLAIMER = (
    "AI predictions are for educational analysis only. Markets are inherently unpredictable; "
    "do not base financial decisions solely on these outputs."
)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fetch_ohlcv(symbol: str, period: str = "5y") -> pd.DataFrame:
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"No data for {symbol}")
    df.columns = df.columns.str.lower()
    return df[["open", "high", "low", "close", "volume"]].dropna()


class EnsemblePredictor:
    """All models for ONE symbol."""

    def __init__(self, symbol: str, model_dir: str = MODEL_DIR):
        self.symbol = symbol.upper()
        self.base = os.path.join(model_dir, self.symbol)
        self.lstm = LSTMStockPredictor() if TF_AVAILABLE else None
        self.xgb = XGBoostPredictor()
        self.arima = ARIMAPredictor()
        self.lock = threading.Lock()
        self._load()

    def _models(self):
        return [m for m in (self.lstm, self.xgb, self.arima) if m is not None]

    def _load(self) -> None:
        for model, sub in ((self.lstm, "lstm"), (self.xgb, "xgb"), (self.arima, "arima")):
            if model is None:
                continue
            path = os.path.join(self.base, sub)
            if os.path.isdir(path):
                try:
                    model.load(path)
                except Exception as e:
                    logger.warning("Could not load %s for %s: %s", sub, self.symbol, e)

    def train_all(self, period: str = "5y") -> Dict[str, Any]:
        df = build_features(fetch_ohlcv(self.symbol, period))
        report: Dict[str, Any] = {}
        with self.lock:
            for model, sub in ((self.lstm, "lstm"), (self.xgb, "xgb"), (self.arima, "arima")):
                if model is None:
                    report["LSTM"] = "skipped (TensorFlow not installed)"
                    continue
                try:
                    if isinstance(model, ARIMAPredictor):
                        report[model.name] = model.train(df)
                    else:
                        report[model.name] = model.train(df, FEATURE_COLUMNS)
                    model.save(os.path.join(self.base, sub))
                except Exception as e:
                    logger.error("%s training failed for %s: %s", model.name, self.symbol, e)
                    report[model.name] = f"failed: {e}"
        return report

    @staticmethod
    def _weight(model) -> float:
        skill = (getattr(model, "metrics", {}) or {}).get("rmse_vs_naive")
        if not skill:
            return 1.0
        return 1.0 / max(skill, 0.5) ** 2

    def predict(self) -> Dict[str, Any]:
        raw = fetch_ohlcv(self.symbol, "2y")
        df = build_features(raw)
        if df.empty:
            raise ValueError(f"Not enough history for {self.symbol}")
        current = float(raw["close"].iloc[-1])
        log_ret = np.log(raw["close"]).diff().dropna()
        sigma = float(log_ret.iloc[-60:].std()) or 1e-4

        preds: Dict[str, Dict[str, Any]] = {}
        with self.lock:
            for model in self._models():
                if model is not self.arima and not model.is_trained:
                    continue
                try:
                    r = model.predict_return(df)
                    preds[model.name] = {"return": r, "weight": self._weight(model), "metrics": getattr(model, "metrics", {})}
                except Exception as e:
                    logger.warning("%s predict failed for %s: %s", model.name, self.symbol, e)
            try:
                week_path = self.arima.forecast_returns(df, steps=5)
            except Exception:
                week_path = None
        if not preds:
            raise RuntimeError("All models failed to predict")

        total_w = sum(p["weight"] for p in preds.values())
        mu = sum(p["return"] * p["weight"] for p in preds.values()) / total_w
        mu_week = float(np.sum(week_path)) if week_path is not None else mu * 5
        p_up = _normal_cdf(mu / sigma)
        p_up_week = _normal_cdf(mu_week / (sigma * math.sqrt(5)))
        next_day = current * math.exp(mu)
        next_week = current * math.exp(mu_week)
        direction = "BULLISH" if mu > 0 else "BEARISH" if mu < 0 else "NEUTRAL"
        agreement = np.mean([np.sign(p["return"]) == np.sign(mu) for p in preds.values()])

        return {
            "symbol": self.symbol,
            "current_price": round(current, 4),
            "predictions": {
                "next_day": {
                    "price": round(next_day, 4),
                    "change": round(next_day - current, 4),
                    "change_pct": round((math.exp(mu) - 1) * 100, 4),
                    "direction": direction,
                    "confidence": round(abs(p_up - 0.5) * 2, 3),
                    "bullish_probability": round(p_up, 3),
                    "bearish_probability": round(1 - p_up, 3),
                    "expected_volatility_pct": round(sigma * 100, 3),
                    "model_agreement": round(float(agreement), 3),
                },
                "next_week": {
                    "price": round(next_week, 4),
                    "change_pct": round((math.exp(mu_week) - 1) * 100, 4),
                    "direction": "BULLISH" if mu_week > 0 else "BEARISH",
                    "bullish_probability": round(p_up_week, 3),
                },
            },
            "model_breakdown": {
                name: {
                    "predicted_price": round(current * math.exp(p["return"]), 4),
                    "change_pct": round((math.exp(p["return"]) - 1) * 100, 4),
                    "direction": "BULLISH" if p["return"] > 0 else "BEARISH",
                    "weight": round(p["weight"] / total_w, 3),
                    "holdout_metrics": p["metrics"],
                }
                for name, p in preds.items()
            },
            "ensemble_method": "skill_weighted_returns",
            "models_used": list(preds),
            "disclaimer": DISCLAIMER,
        }


class PredictorRegistry:
    """Per-symbol predictors with a small LRU cache."""

    def __init__(self, max_symbols: int = 50):
        self.max_symbols = max_symbols
        self._items: "OrderedDict[str, EnsemblePredictor]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, symbol: str) -> EnsemblePredictor:
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._items:
                self._items.move_to_end(symbol)
                return self._items[symbol]
            predictor = EnsemblePredictor(symbol)
            self._items[symbol] = predictor
            if len(self._items) > self.max_symbols:
                self._items.popitem(last=False)
            return predictor

    def reload(self, symbol: str) -> Optional[EnsemblePredictor]:
        with self._lock:
            self._items.pop(symbol.upper(), None)
        return self.get(symbol)


registry = PredictorRegistry()
