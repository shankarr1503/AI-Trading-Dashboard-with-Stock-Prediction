"""
XGBoost next-day return predictor.

Data is split chronologically into train / early-stopping / holdout segments;
the scaler is fit on the training segment only, so no information from the
validation or holdout periods leaks into training.
"""
import logging
import os

import joblib
import pandas as pd
from sklearn.preprocessing import StandardScaler

from ml.evaluation.metrics import compute_metrics

logger = logging.getLogger(__name__)

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False
    logger.warning("XGBoost not available.")


class XGBoostPredictor:
    name = "XGBoost"

    def __init__(self, n_estimators: int = 600, learning_rate: float = 0.03, max_depth: int = 3):
        self.params = dict(n_estimators=n_estimators, learning_rate=learning_rate, max_depth=max_depth)
        self.scaler = StandardScaler()
        self.model = None
        self.feature_columns: list = []
        self.metrics: dict = {}
        self.is_trained = False

    def train(self, df: pd.DataFrame, feature_cols: list):
        if not XGB_AVAILABLE:
            raise RuntimeError("XGBoost not installed.")
        data = df.dropna(subset=feature_cols + ["target"])
        n = len(data)
        if n < 200:
            raise ValueError(f"Need at least 200 labelled rows, have {n}")
        i_val, i_test = int(n * 0.70), int(n * 0.85)
        train, val, test = data.iloc[:i_val], data.iloc[i_val:i_test], data.iloc[i_test:]

        self.feature_columns = list(feature_cols)
        X_train = self.scaler.fit_transform(train[feature_cols].values)
        X_val = self.scaler.transform(val[feature_cols].values)
        X_test = self.scaler.transform(test[feature_cols].values)

        self.model = xgb.XGBRegressor(
            **self.params,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=10, reg_lambda=5.0,
            tree_method="hist", eval_metric="rmse", early_stopping_rounds=50, verbosity=0,
        )
        self.model.fit(X_train, train["target"].values, eval_set=[(X_val, val["target"].values)], verbose=False)
        self.is_trained = True
        self.metrics = compute_metrics(test["target"].values, self.model.predict(X_test))
        logger.info("XGBoost trained; holdout metrics: %s", self.metrics)
        return self.metrics

    def predict_return(self, df: pd.DataFrame) -> float:
        if not self.is_trained or self.model is None:
            raise RuntimeError("Model not trained.")
        X = self.scaler.transform(df[self.feature_columns].values[-1:, :])
        return float(self.model.predict(X)[0])

    def feature_importance(self, top: int = 10) -> dict:
        if self.model is None:
            return {}
        pairs = sorted(zip(self.feature_columns, self.model.feature_importances_), key=lambda x: x[1], reverse=True)
        return {k: round(float(v), 4) for k, v in pairs[:top]}

    def save(self, path: str):
        os.makedirs(path, exist_ok=True)
        joblib.dump(
            {"model": self.model, "scaler": self.scaler, "features": self.feature_columns, "metrics": self.metrics},
            os.path.join(path, "xgb.joblib"),
        )

    def load(self, path: str):
        bundle = joblib.load(os.path.join(path, "xgb.joblib"))
        self.model, self.scaler = bundle["model"], bundle["scaler"]
        self.feature_columns, self.metrics = bundle["features"], bundle.get("metrics", {})
        self.is_trained = True
