"""ML service tests (skipped when the ML stack isn't installed)."""
import pytest

pytest.importorskip("xgboost")
pytest.importorskip("statsmodels")

from fastapi.testclient import TestClient  # noqa: E402

import ml.ensemble.predictor as ep  # noqa: E402
from ml.evaluation.metrics import compute_metrics  # noqa: E402
from ml.feature_engineering.pipeline import FEATURE_COLUMNS, build_features  # noqa: E402
from tests.conftest import make_ohlcv  # noqa: E402


def test_features_are_causal_and_target_is_next_return():
    df = make_ohlcv(1, n=400)
    full = build_features(df, dropna=False)
    part = build_features(df.iloc[:300], dropna=False)
    assert full[FEATURE_COLUMNS].iloc[:300].equals(part[FEATURE_COLUMNS]) or \
        (full[FEATURE_COLUMNS].iloc[:300] - part[FEATURE_COLUMNS]).abs().max().max() < 1e-12
    assert full["target"].isna().iloc[-1]


def test_directional_accuracy_uses_signs():
    m = compute_metrics([0.01, -0.02, 0.03, -0.01], [0.005, -0.001, -0.002, -0.004])
    assert m["directional_accuracy_pct"] == 75.0


def test_per_symbol_models_and_arima_fallback(tmp_path, monkeypatch):
    frames = {"AAA": make_ohlcv(1, n=900), "BBB": make_ohlcv(2, n=900)}
    monkeypatch.setattr(ep, "fetch_ohlcv", lambda s, period="5y": frames[s])
    a = ep.EnsemblePredictor("AAA", model_dir=str(tmp_path))
    assert a.predict()["models_used"] == ["ARIMA"]  # untrained → ARIMA only, no crash
    report = a.train_all()
    assert "rmse_vs_naive" in report["XGBoost"]
    b = ep.EnsemblePredictor("BBB", model_dir=str(tmp_path))
    assert not b.xgb.is_trained  # AAA's models never leak into BBB
    out = ep.EnsemblePredictor("AAA", model_dir=str(tmp_path)).predict()
    assert set(out["models_used"]) == {"XGBoost", "ARIMA"}
    p = out["predictions"]["next_day"]["bullish_probability"]
    assert 0.3 < p < 0.7  # next-day direction is close to a coin flip on noise


def test_training_endpoint_requires_key(monkeypatch):
    import ml.api.server as srv

    c = TestClient(srv.app)
    assert c.post("/train/AAPL").status_code == 403
    monkeypatch.setattr(srv, "API_KEY", "secret")
    assert c.post("/train/AAPL", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/predict/bad$sym").status_code == 422
