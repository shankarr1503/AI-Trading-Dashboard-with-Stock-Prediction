"""
Model Evaluation Metrics.
Computes MAE, RMSE, MAPE, and Directional Accuracy for prediction assessment.
"""
import numpy as np
from typing import List, Dict


def compute_metrics(y_true: List[float], y_pred: List[float]) -> Dict[str, float]:
    """
    Compute standard regression and directional metrics.

    Args:
        y_true: Actual prices
        y_pred: Predicted prices

    Returns:
        Dict with MAE, RMSE, MAPE, Directional Accuracy
    """
    y_true = np.array(y_true)
    y_pred = np.array(y_pred)

    mae = float(np.mean(np.abs(y_true - y_pred)))
    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))

    # MAPE — avoid division by zero
    non_zero = y_true != 0
    mape = float(np.mean(np.abs((y_true[non_zero] - y_pred[non_zero]) / y_true[non_zero])) * 100)

    # Directional accuracy — did the model predict up/down correctly?
    if len(y_true) > 1:
        actual_dirs = np.diff(y_true) > 0
        pred_dirs = np.diff(y_pred) > 0
        dir_acc = float(np.mean(actual_dirs == pred_dirs) * 100)
    else:
        dir_acc = 0.0

    return {
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "mape_pct": round(mape, 4),
        "directional_accuracy_pct": round(dir_acc, 2),
    }
