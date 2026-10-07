"""
LSTM next-day return predictor.

Architecture: Input(seq_len, n_features) → LSTM(64, seq) → Dropout → LSTM(32)
→ Dropout → Dense(16, relu) → Dense(1).

Leakage controls: the feature scaler is fit on the training segment only,
the target is the next-day return (not a scaled price), and validation /
holdout segments are strictly later in time than the training data.
"""
import logging
import os
from typing import Optional, Tuple

import joblib
import pandas as pd
from sklearn.preprocessing import StandardScaler

from ml.evaluation.metrics import compute_metrics
from ml.feature_engineering.pipeline import create_sequences

logger = logging.getLogger(__name__)

try:
    import tensorflow as tf  # noqa: F401
    from tensorflow import keras
    from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau
    from tensorflow.keras.layers import LSTM, Dense, Dropout, Input
    from tensorflow.keras.models import Sequential, load_model
    TF_AVAILABLE = True
except ImportError:
    TF_AVAILABLE = False
    logger.warning("TensorFlow not available. LSTM model will be disabled.")


class LSTMStockPredictor:
    name = "LSTM"

    def __init__(self, sequence_length: int = 60, units: Tuple[int, int] = (64, 32), dropout: float = 0.2,
                 learning_rate: float = 1e-3):
        self.sequence_length = sequence_length
        self.units = units
        self.dropout = dropout
        self.learning_rate = learning_rate
        self.scaler = StandardScaler()
        self.target_scale = 1.0
        self.model: Optional[object] = None
        self.feature_columns: list = []
        self.metrics: dict = {}
        self.is_trained = False

    def _build(self, n_features: int):
        model = Sequential([
            Input(shape=(self.sequence_length, n_features)),
            LSTM(self.units[0], return_sequences=True),
            Dropout(self.dropout),
            LSTM(self.units[1]),
            Dropout(self.dropout),
            Dense(16, activation="relu"),
            Dense(1),
        ])
        model.compile(optimizer=keras.optimizers.Adam(learning_rate=self.learning_rate), loss="huber", metrics=["mae"])
        return model

    def train(self, df: pd.DataFrame, feature_cols: list, epochs: int = 50, batch_size: int = 32):
        if not TF_AVAILABLE:
            raise RuntimeError("TensorFlow not installed.")
        data = df.dropna(subset=feature_cols + ["target"])
        n = len(data)
        if n < self.sequence_length * 4:
            raise ValueError(f"Need at least {self.sequence_length * 4} labelled rows, have {n}")
        self.feature_columns = list(feature_cols)
        i_val, i_test = int(n * 0.70), int(n * 0.85)

        self.scaler.fit(data[feature_cols].iloc[:i_val].values)
        self.target_scale = float(data["target"].iloc[:i_val].std()) or 1.0
        X_all = self.scaler.transform(data[feature_cols].values)
        y_all = data["target"].values / self.target_scale

        def segment(a: int, b: int):
            start = max(0, a - self.sequence_length + 1)
            X, y = create_sequences(X_all[start:b], y_all[start:b], self.sequence_length)
            return X, y

        X_tr, y_tr = segment(0, i_val)
        X_va, y_va = segment(i_val, i_test)
        X_te, y_te = segment(i_test, n)

        self.model = self._build(len(feature_cols))
        self.model.fit(
            X_tr, y_tr, validation_data=(X_va, y_va), epochs=epochs, batch_size=batch_size, verbose=0,
            callbacks=[
                EarlyStopping(monitor="val_loss", patience=8, restore_best_weights=True),
                ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=4, min_lr=1e-5),
            ],
        )
        self.is_trained = True
        pred = self.model.predict(X_te, verbose=0)[:, 0] * self.target_scale
        self.metrics = compute_metrics(y_te * self.target_scale, pred)
        logger.info("LSTM trained; holdout metrics: %s", self.metrics)
        return self.metrics

    def predict_return(self, df: pd.DataFrame) -> float:
        if not self.is_trained or self.model is None:
            raise RuntimeError("Model not trained.")
        window = df[self.feature_columns].dropna().values[-self.sequence_length:]
        if len(window) < self.sequence_length:
            raise ValueError("Not enough rows for an LSTM window")
        seq = self.scaler.transform(window).reshape(1, self.sequence_length, -1)
        return float(self.model.predict(seq, verbose=0)[0, 0]) * self.target_scale

    def save(self, path: str):
        os.makedirs(path, exist_ok=True)
        if self.model is not None:
            self.model.save(os.path.join(path, "lstm_model.keras"))
        joblib.dump(
            {"scaler": self.scaler, "features": self.feature_columns, "target_scale": self.target_scale,
             "metrics": self.metrics, "sequence_length": self.sequence_length},
            os.path.join(path, "lstm_meta.joblib"),
        )

    def load(self, path: str):
        if not TF_AVAILABLE:
            raise RuntimeError("TensorFlow not installed.")
        meta = joblib.load(os.path.join(path, "lstm_meta.joblib"))
        self.model = load_model(os.path.join(path, "lstm_model.keras"))
        self.scaler, self.feature_columns = meta["scaler"], meta["features"]
        self.target_scale, self.metrics = meta["target_scale"], meta.get("metrics", {})
        self.sequence_length = meta.get("sequence_length", self.sequence_length)
        self.is_trained = True
