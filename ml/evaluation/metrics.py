"""
Model evaluation metrics for next-day return forecasts.
"""
from typing import Dict, Sequence

import numpy as np


def compute_metrics(y_true: Sequence[float], y_pred: Sequence[float]) -> Dict[str, float]:
    """
    y_true / y_pred are next-day *returns*. Directional accuracy is the share
    of days where the predicted sign matches the realised sign (days with a
    zero prediction or zero return are excluded). A model with no skill scores
    ~50%; anything far above that on out-of-sample data deserves suspicion.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    err = y_true - y_pred
    mae = float(np.mean(np.abs(err))) if len(err) else 0.0
    rmse = float(np.sqrt(np.mean(err ** 2))) if len(err) else 0.0
    # Skill vs. the naive "tomorrow's return is zero" forecast.
    naive_rmse = float(np.sqrt(np.mean(y_true ** 2))) if len(y_true) else 0.0
    mask = (y_true != 0) & (y_pred != 0)
    dir_acc = float(np.mean(np.sign(y_true[mask]) == np.sign(y_pred[mask])) * 100) if mask.any() else 0.0
    return {
        "mae": round(mae, 6),
        "rmse": round(rmse, 6),
        "rmse_vs_naive": round(rmse / naive_rmse, 4) if naive_rmse else 0.0,
        "directional_accuracy_pct": round(dir_acc, 2),
        "n": int(len(y_true)),
    }
