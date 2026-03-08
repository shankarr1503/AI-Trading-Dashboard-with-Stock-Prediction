"""
XGBoost Stock Price Predictor.
Uses gradient-boosted trees with financial feature set.
"""
import os
import logging
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_absolute_error, mean_squared_error
import joblib

logger = logging.getLogger(__name__)

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False
    logger.warning("XGBoost not available.")


class XGBoostPredictor:
    """XGBoost regression model for next-day price prediction."""

    def __init__(self, n_estimators: int = 500, learning_rate: float = 0.05, max_depth: int = 6):
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.scaler = StandardScaler()
        self.model = None
        self.feature_columns = []
        self.is_trained = False

    def _prepare(self, df: pd.DataFrame, feature_cols: list, target_col: str = "close", shift: int = 1):
        """Prepare X (features) and y (next-day close)."""
        self.feature_columns = feature_cols
        X = df[feature_cols].values
        y = df[target_col].shift(-shift).dropna().values
        X = X[:-shift]
        X_scaled = self.scaler.fit_transform(X)
        return X_scaled, y

    def train(self, df: pd.DataFrame, feature_cols: list):
        """Train XGBoost model with time-series cross-validation."""
        if not XGB_AVAILABLE:
            raise RuntimeError("XGBoost not installed.")

        X, y = self._prepare(df, feature_cols)
        self.model = xgb.XGBRegressor(
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            max_depth=self.max_depth,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=3,
            gamma=0.1,
            tree_method="hist",
            eval_metric="mae",
            early_stopping_rounds=50,
            verbosity=0,
        )

        # Time-series split validation
        tscv = TimeSeriesSplit(n_splits=5)
        train_idx, val_idx = list(tscv.split(X))[-1]  # use last fold

        self.model.fit(
            X[train_idx], y[train_idx],
            eval_set=[(X[val_idx], y[val_idx])],
            verbose=False,
        )
        self.is_trained = True

        val_pred = self.model.predict(X[val_idx])
        mae = mean_absolute_error(y[val_idx], val_pred)
        rmse = np.sqrt(mean_squared_error(y[val_idx], val_pred))
        logger.info(f"XGBoost trained. Val MAE: {mae:.4f}, RMSE: {rmse:.4f}")

    def predict(self, df: pd.DataFrame) -> dict:
        """Predict next-day close price."""
        if not self.is_trained or self.model is None:
            raise RuntimeError("Model not trained.")

        X = df[self.feature_columns].values[-1:, :]
        X_scaled = self.scaler.transform(X)
        pred = float(self.model.predict(X_scaled)[0])
        current = float(df["close"].iloc[-1])
        change_pct = (pred - current) / current * 100

        return {
            "model": "XGBoost",
            "predicted_price": round(pred, 4),
            "current_price": round(current, 4),
            "change_pct": round(change_pct, 4),
            "direction": "BULLISH" if change_pct > 0 else "BEARISH",
            "feature_importance": dict(
                sorted(
                    zip(self.feature_columns, self.model.feature_importances_),
                    key=lambda x: x[1],
                    reverse=True,
                )[:10]
            ),
        }

    def save(self, path: str):
        os.makedirs(path, exist_ok=True)
        joblib.dump(self.model, os.path.join(path, "xgb_model.pkl"))
        joblib.dump(self.scaler, os.path.join(path, "xgb_scaler.pkl"))
        joblib.dump(self.feature_columns, os.path.join(path, "xgb_features.pkl"))

    def load(self, path: str):
        self.model = joblib.load(os.path.join(path, "xgb_model.pkl"))
        self.scaler = joblib.load(os.path.join(path, "xgb_scaler.pkl"))
        self.feature_columns = joblib.load(os.path.join(path, "xgb_features.pkl"))
        self.is_trained = True
