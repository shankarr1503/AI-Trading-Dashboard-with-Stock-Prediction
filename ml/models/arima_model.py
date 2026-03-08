"""
ARIMA / SARIMAX Time-Series Model for Stock Price Forecasting.
Uses statsmodels auto-ARIMA-style fitting with AIC-based order selection.
"""
import os
import logging
import numpy as np
import pandas as pd
from typing import List, Tuple, Optional
import warnings
import joblib

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

try:
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    from statsmodels.tsa.stattools import adfuller
    STATSMODELS_AVAILABLE = True
except ImportError:
    STATSMODELS_AVAILABLE = False
    logger.warning("statsmodels not available. ARIMA model will be disabled.")


class ARIMAPredictor:
    """ARIMA time-series predictor for stock close price."""

    ORDERS_TO_TRY = [
        (1, 1, 1), (2, 1, 2), (1, 1, 2), (2, 1, 1),
        (3, 1, 0), (0, 1, 3), (1, 2, 1),
    ]

    def __init__(self):
        self.model_fit = None
        self.best_order = (1, 1, 1)
        self.close_series: Optional[pd.Series] = None
        self.is_trained = False

    def _is_stationary(self, series: pd.Series, threshold: float = 0.05) -> bool:
        result = adfuller(series.dropna())
        return result[1] < threshold  # p-value < 0.05 → stationary

    def _select_best_order(self, series: pd.Series) -> Tuple[int, int, int]:
        """Select ARIMA order by minimizing AIC over candidate orders."""
        best_aic = np.inf
        best_order = (1, 1, 1)
        for order in self.ORDERS_TO_TRY:
            try:
                mdl = ARIMA(series, order=order).fit(method_kwargs={"warn_convergence": False})
                if mdl.aic < best_aic:
                    best_aic = mdl.aic
                    best_order = order
            except Exception:
                continue
        return best_order

    def train(self, df: pd.DataFrame):
        """Fit ARIMA model to the close price series."""
        if not STATSMODELS_AVAILABLE:
            raise RuntimeError("statsmodels not installed.")

        series = df["close"].astype(float)
        self.close_series = series
        self.best_order = self._select_best_order(series)
        logger.info(f"Selected ARIMA order: {self.best_order}")

        self.model_fit = ARIMA(series, order=self.best_order).fit()
        self.is_trained = True
        logger.info(f"ARIMA trained. AIC: {self.model_fit.aic:.2f}")

    def predict(self, df: pd.DataFrame, steps: int = 5) -> dict:
        """Generate multi-step forecast."""
        if not self.is_trained or self.model_fit is None:
            raise RuntimeError("Model not trained.")

        # Re-fit on all data to get freshest model
        series = df["close"].astype(float)
        model_fit = ARIMA(series, order=self.best_order).fit()

        forecast = model_fit.forecast(steps=steps)
        current = float(series.iloc[-1])
        next_day_pred = float(forecast.iloc[0])
        next_week_pred = float(forecast.iloc[-1]) if len(forecast) >= 5 else next_day_pred

        change_pct = (next_day_pred - current) / current * 100

        return {
            "model": "ARIMA",
            "order": list(self.best_order),
            "predicted_price": round(next_day_pred, 4),
            "current_price": round(current, 4),
            "change_pct": round(change_pct, 4),
            "direction": "BULLISH" if change_pct > 0 else "BEARISH",
            "forecast": [round(float(v), 4) for v in forecast.values],
            "forecast_days": steps,
        }

    def save(self, path: str):
        os.makedirs(path, exist_ok=True)
        joblib.dump({
            "best_order": self.best_order,
            "is_trained": self.is_trained,
        }, os.path.join(path, "arima_config.pkl"))

    def load(self, path: str):
        config = joblib.load(os.path.join(path, "arima_config.pkl"))
        self.best_order = config["best_order"]
        self.is_trained = True
