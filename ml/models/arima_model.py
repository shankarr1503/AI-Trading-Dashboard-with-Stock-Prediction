"""
ARIMA model on daily log returns.

ARIMA needs no offline training, so it is always available: if no saved order
exists for a symbol, the order is selected by AIC on the spot. Fitting on log
returns (d=0) rather than prices keeps the series stationary.
"""
import logging
import os
import warnings
from typing import Tuple

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from statsmodels.tsa.arima.model import ARIMA
    STATSMODELS_AVAILABLE = True
except ImportError:
    STATSMODELS_AVAILABLE = False
    logger.warning("statsmodels not available. ARIMA model will be disabled.")


class ARIMAPredictor:
    name = "ARIMA"
    ORDERS_TO_TRY = [(0, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 1), (2, 0, 0), (2, 0, 1)]
    LOOKBACK = 500

    def __init__(self):
        self.order: Tuple[int, int, int] | None = None
        self.metrics: dict = {}

    @property
    def is_trained(self) -> bool:
        return self.order is not None

    def _returns(self, df: pd.DataFrame) -> pd.Series:
        r = np.log(df["close"].astype(float)).diff().dropna().iloc[-self.LOOKBACK:]
        return r.reset_index(drop=True)

    def select_order(self, df: pd.DataFrame) -> Tuple[int, int, int]:
        if not STATSMODELS_AVAILABLE:
            raise RuntimeError("statsmodels not installed.")
        r = self._returns(df)
        best_aic, best = np.inf, (0, 0, 0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for order in self.ORDERS_TO_TRY:
                try:
                    aic = ARIMA(r, order=order).fit().aic
                except Exception:
                    continue
                if aic < best_aic:
                    best_aic, best = aic, order
        self.order = best
        return best

    def train(self, df: pd.DataFrame):
        order = self.select_order(df)
        logger.info("ARIMA order selected: %s", order)
        return {"order": list(order)}

    def forecast_returns(self, df: pd.DataFrame, steps: int = 5) -> np.ndarray:
        if not STATSMODELS_AVAILABLE:
            raise RuntimeError("statsmodels not installed.")
        if self.order is None:
            self.select_order(df)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = ARIMA(self._returns(df), order=self.order).fit()
            return np.asarray(fit.forecast(steps=steps), dtype=float)

    def predict_return(self, df: pd.DataFrame) -> float:
        return float(self.forecast_returns(df, steps=1)[0])

    def save(self, path: str):
        os.makedirs(path, exist_ok=True)
        joblib.dump({"order": self.order}, os.path.join(path, "arima.joblib"))

    def load(self, path: str):
        self.order = tuple(joblib.load(os.path.join(path, "arima.joblib"))["order"])
