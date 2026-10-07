"""HTTP-level tests: auth, ownership, admin controls, and market endpoints (offline)."""
import pytest
from fastapi.testclient import TestClient

import backend.alerts.router as alerts_mod
import backend.indicators.service as indicators_mod
import backend.market_data.router as market_router_mod
import backend.portfolio.service as portfolio_mod
import backend.signals.service as signals_mod
import backend.trading.router as bot_router_mod
from backend.main import app
from tests.conftest import FakeMarket, FakePredictor, make_ohlcv, make_trending


@pytest.fixture
def client(db_tables, monkeypatch):
    fake = FakeMarket({"AAPL": make_trending(1), "MSFT": make_ohlcv(2, n=500)})
    for mod in (market_router_mod, indicators_mod, portfolio_mod, signals_mod, bot_router_mod, alerts_mod):
        monkeypatch.setattr(mod, "market_data_service", fake)
    monkeypatch.setattr(signals_mod, "prediction_service", FakePredictor())
    with TestClient(app) as c:
        yield c


def register(c, name, password="correct-horse-battery"):
    r = c.post("/auth/register", json={"email": f"{name}@example.com", "username": name, "password": password})
    assert r.status_code == 201, r.text
    tok = c.post("/auth/login", json={"email": f"{name}@example.com", "password": password}).json()
    return r.json(), {"Authorization": f"Bearer {tok['access_token']}"}, tok


def test_health(client):
    assert client.get("/health").json()["status"] == "healthy"


def test_first_user_is_admin_and_me_works(client):
    alice, h_alice, _ = register(client, "alice")
    bob, h_bob, _ = register(client, "bob")
    assert alice["is_superuser"] and not bob["is_superuser"]
    assert client.get("/auth/me", headers=h_bob).json()["username"] == "bob"
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_login_rejects_bad_password_and_refresh_works(client):
    _, _, tok = register(client, "carol")
    bad = client.post("/auth/login", json={"email": "carol@example.com", "password": "wrong-password"})
    assert bad.status_code == 401
    new = client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert new.status_code == 200
    # an access token can't be used as a refresh token
    assert client.post("/auth/refresh", json={"refresh_token": tok["access_token"]}).status_code == 401


def test_portfolio_is_private(client):
    _, h_a, _ = register(client, "alice")
    _, h_b, _ = register(client, "bob")
    pid = client.post("/api/portfolio/", json={"name": "A"}, headers=h_a).json()["id"]
    assert client.post(f"/api/portfolio/{pid}/holdings", headers=h_a,
                       json={"symbol": "aapl", "quantity": 10, "avg_buy_price": 100}).status_code == 201
    assert client.get(f"/api/portfolio/{pid}/summary", headers=h_b).status_code == 404
    assert client.post(f"/api/portfolio/{pid}/holdings", headers=h_b,
                       json={"symbol": "MSFT", "quantity": 1, "avg_buy_price": 1}).status_code == 404
    assert client.get(f"/api/portfolio/{pid}/summary").status_code == 401
    summary = client.get(f"/api/portfolio/{pid}/summary", headers=h_a).json()
    assert summary["holdings"][0]["symbol"] == "AAPL"
    assert summary["total_value"] > 0
    assert client.get("/api/portfolio/", headers=h_b).json() == []


def test_alerts_are_private_and_validated(client):
    _, h_a, _ = register(client, "alice")
    _, h_b, _ = register(client, "bob")
    assert client.post("/api/alerts/", headers=h_a, json={"symbol": "AAPL", "alert_type": "PRICE_ABOVE"}).status_code == 400
    a = client.post("/api/alerts/", headers=h_a, json={"symbol": "AAPL", "alert_type": "PRICE_ABOVE", "threshold_value": 1})
    assert a.status_code == 201
    aid = a.json()["id"]
    assert client.delete(f"/api/alerts/{aid}", headers=h_b).status_code == 404
    assert client.get("/api/alerts/", headers=h_b).json() == []
    checked = client.post("/api/alerts/check", headers=h_a).json()
    assert checked["checked"] == 1 and checked["triggered"][0]["alert_id"] == aid


def test_bot_controls_require_admin(client):
    _, h_admin, _ = register(client, "admin")
    _, h_user, _ = register(client, "user")
    assert client.get("/api/bot/status").status_code == 401
    status = client.get("/api/bot/status", headers=h_user).json()
    assert status["mode"] == "paper" and status["enabled"] is False and status["live_trading"] is False
    assert client.post("/api/bot/start", headers=h_user).status_code == 403
    assert client.post("/api/bot/start", headers=h_admin).json() == {"enabled": True}
    assert client.post("/api/bot/stop", headers=h_admin).json() == {"enabled": False}
    bad = client.put("/api/bot/config", headers=h_admin, json={"risk": {"max_risk_per_trade_pct": 0.5}})
    assert bad.status_code == 422
    ok = client.put("/api/bot/config", headers=h_admin, json={"risk": {"max_risk_per_trade_pct": 0.005}, "universe": ["aapl"]})
    assert ok.status_code == 200 and ok.json()["universe"] == ["AAPL"]
    assert ok.json()["risk_config"]["max_risk_per_trade_pct"] == 0.005


def test_market_and_indicator_endpoints(client):
    assert client.get("/api/market/quote/AAPL").json()["current_price"] > 0
    assert client.get("/api/market/quote/NOPE").status_code == 404
    assert client.get("/api/market/quote/bad$sym").status_code == 404
    batch = client.get("/api/market/batch?symbols=AAPL,MSFT,NOPE").json()
    assert "error" in batch["NOPE"] and batch["AAPL"]["current_price"] > 0
    ind = client.get("/api/indicators/AAPL").json()
    assert ind["current"]["RSI_14"] is not None and "RSI" in ind["signals"]
    assert len(ind["indicators"]["SMA_20"]) == len(ind["timestamps"])


def test_signal_and_bot_analysis(client):
    _, h, _ = register(client, "alice")
    sig = client.get("/api/signals/AAPL").json()
    assert sig["signal"] in ("BUY", "SELL", "HOLD")
    assert sig["round_trip_cost_pct"] > 0
    assert "edge" in sig and "worth_the_costs" in sig
    assert client.get("/api/bot/analyze/AAPL", headers=h).json()["symbol"] == "AAPL"


def test_backtest_endpoint(client):
    _, h, _ = register(client, "alice")
    r = client.post("/api/bot/backtest", headers=h, json={"symbols": ["AAPL", "MSFT"], "walk_forward": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "metrics" in body and "benchmark" in body and body["symbols"] == ["AAPL", "MSFT"]
