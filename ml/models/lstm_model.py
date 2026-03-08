"""
LSTM Deep Learning Model for Stock Price Prediction.

Architecture:
  Input (seq_len, n_features)
    → LSTM(128, return_sequences=True)
    → Dropout(0.20)
    → LSTM(64, return_sequences=True)
    → Dropout(0.20)
    → LSTM(32)
    → Dropout(0.15)
    → Dense(16, activation='relu')
    → Dense(1)  ← predicted next close price

Training pipeline:
  - MinMaxScaler normalization
  - 60-day sliding window sequences
  - 80/20 train-val split
  - EarlyStopping + ReduceLROnPlateau callbacks
"""
import os
import logging
import numpy as np
import pandas as pd
from typing import Tuple, Optional
from sklearn.preprocessing import MinMaxScaler
import joblib

logger = logging.getLogger(__name__)

try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras.models import Sequential, load_model
    from tensorflow.keras.layers import LSTM, Dense, Dropout, Input
    from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau, ModelCheckpoint
    TF_AVAILABLE = True
except ImportError:
    TF_AVAILABLE = False
    logger.warning("TensorFlow not available. LSTM model will be disabled.")


class LSTMStockPredictor:
    """LSTM-based deep learning stock price predictor."""

    def __init__(
        self,
        sequence_length: int = 60,
        lstm_units: Tuple[int, int, int] = (128, 64, 32),
        dropout_rate: float = 0.20,
        learning_rate: float = 0.001,
    ):
        self.sequence_length = sequence_length
        self.lstm_units = lstm_units
        self.dropout_rate = dropout_rate
        self.learning_rate = learning_rate
        self.scaler = MinMaxScaler(feature_range=(0, 1))
        self.model: Optional[object] = None
        self.is_trained = False
        self.feature_columns = []

    def _build_model(self, n_features: int) -> "keras.Model":
        """Build the LSTM architecture."""
        model = Sequential([
            Input(shape=(self.sequence_length, n_features)),
            LSTM(self.lstm_units[0], return_sequences=True),
            Dropout(self.dropout_rate),
            LSTM(self.lstm_units[1], return_sequences=True),
            Dropout(self.dropout_rate),
            LSTM(self.lstm_units[2], return_sequences=False),
            Dropout(self.dropout_rate * 0.75),
            Dense(16, activation="relu"),
            Dense(1),
        ])
        model.compile(
            optimizer=keras.optimizers.Adam(learning_rate=self.learning_rate),
            loss="huber",  # Robust to outliers
            metrics=["mae"],
        )
        return model

    def _prepare_data(
        self, df: pd.DataFrame, feature_cols: list
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Scale and create sequences."""
        self.feature_columns = feature_cols
        data = df[feature_cols].values
        scaled = self.scaler.fit_transform(data)

        X, y = [], []
        close_idx = feature_cols.index("close") if "close" in feature_cols else 0
        for i in range(self.sequence_length, len(scaled)):
            X.append(scaled[i - self.sequence_length:i])
            y.append(scaled[i, close_idx])

        X, y = np.array(X), np.array(y)
        split = int(len(X) * 0.80)
        return X[:split], X[split:], y[:split], y[split:]

    def train(self, df: pd.DataFrame, feature_cols: list, epochs: int = 50, batch_size: int = 32):
        """Train the LSTM model."""
        if not TF_AVAILABLE:
            raise RuntimeError("TensorFlow not installed.")

        X_train, X_val, y_train, y_val = self._prepare_data(df, feature_cols)
        self.model = self._build_model(n_features=len(feature_cols))

        callbacks = [
            EarlyStopping(monitor="val_loss", patience=10, restore_best_weights=True),
            ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=5, min_lr=1e-6),
        ]

        history = self.model.fit(
            X_train, y_train,
            validation_data=(X_val, y_val),
            epochs=epochs,
            batch_size=batch_size,
            callbacks=callbacks,
            verbose=1,
        )
        self.is_trained = True
        logger.info(f"LSTM trained. Val MAE: {min(history.history['val_mae']):.4f}")
        return history

    def predict(self, df: pd.DataFrame) -> dict:
        """Predict next-day close price."""
        if not self.is_trained or self.model is None:
            raise RuntimeError("Model not trained. Run train() first.")

        data = df[self.feature_columns].values
        scaled = self.scaler.transform(data)
        seq = scaled[-self.sequence_length:].reshape(1, self.sequence_length, -1)

        pred_scaled = self.model.predict(seq, verbose=0)[0, 0]

        # Inverse transform (only the close column)
        close_idx = self.feature_columns.index("close") if "close" in self.feature_columns else 0
        dummy = np.zeros((1, len(self.feature_columns)))
        dummy[0, close_idx] = pred_scaled
        pred_price = self.scaler.inverse_transform(dummy)[0, close_idx]

        current_price = df["close"].iloc[-1]
        change_pct = (pred_price - current_price) / current_price * 100

        return {
            "model": "LSTM",
            "predicted_price": round(float(pred_price), 4),
            "current_price": round(float(current_price), 4),
            "change_pct": round(float(change_pct), 4),
            "direction": "BULLISH" if change_pct > 0 else "BEARISH",
        }

    def save(self, path: str):
        """Save model and scaler."""
        os.makedirs(path, exist_ok=True)
        if self.model:
            self.model.save(os.path.join(path, "lstm_model.keras"))
        joblib.dump(self.scaler, os.path.join(path, "lstm_scaler.pkl"))
        joblib.dump(self.feature_columns, os.path.join(path, "lstm_features.pkl"))

    def load(self, path: str):
        """Load model and scaler from disk."""
        if not TF_AVAILABLE:
            raise RuntimeError("TensorFlow not installed.")
        self.model = load_model(os.path.join(path, "lstm_model.keras"))
        self.scaler = joblib.load(os.path.join(path, "lstm_scaler.pkl"))
        self.feature_columns = joblib.load(os.path.join(path, "lstm_features.pkl"))
        self.is_trained = True
