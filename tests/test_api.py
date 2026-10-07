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
    # The bot's book is private to the administrator.
    for path in ("/api/bot/status", "/api/bot/positions", "/api/bot/trades", "/api/bot/decisions", "/api/bot/equity"):
        assert client.get(path, headers=h_user).status_code == 403
    status = client.get("/api/bot/status", headers=h_admin).json()
    assert status["mode"] == "paper" and status["enabled"] is False and status["live_trading"] is False
    assert status["stale"] is True and status["calibration"] is None
    assert client.post("/api/bot/start", headers=h_user).status_code == 403
    assert client.post("/api/bot/reset-halt", headers=h_admin).status_code == 409   # not halted
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
    _, h_user, _ = register(client, "bob")
    assert client.post("/api/bot/backtest", headers=h_user, json={"symbols": ["AAPL"]}).status_code == 403
    r = client.post("/api/bot/backtest", headers=h, json={"symbols": ["AAPL", "MSFT"], "walk_forward": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "metrics" in body and "benchmark" in body and body["symbols"] == ["AAPL", "MSFT"]


def test_bot_health_endpoint(client):
    r = client.get("/api/bot/health")
    assert r.status_code == 503 and r.json()["healthy"] is False   # no cycle has run yet


def test_registration_never_grants_admin_outside_development(client, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    first, _, _ = register(client, "mallory")
    assert not first["is_superuser"]          # an email address proves nothing: admins come from the CLI
    r = client.post("/auth/register", json={"email": "MALLORY@example.com", "username": "mallory2", "password": "x" * 12})
    assert r.status_code == 400               # case variants are the same account
    monkeypatch.setattr(settings, "APP_ENV", "test")
    monkeypatch.setattr(settings, "REGISTRATION_OPEN", False)
    r = client.post("/auth/register", json={"email": "late@example.com", "username": "late", "password": "x" * 12})
    assert r.status_code == 403


def test_logout_all_revokes_tokens(client):
    _, h, tok = register(client, "alice")
    assert client.get("/auth/me", headers=h).status_code == 200
    assert client.post("/auth/logout-all", headers=h).status_code == 200
    assert client.get("/auth/me", headers=h).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 401


def test_duplicate_registration_does_not_leak_which_field(client):
    register(client, "alice")
    r = client.post("/auth/register", json={"email": "alice@example.com", "username": "other1", "password": "x" * 12})
    assert r.status_code == 400 and "email" not in r.json()["detail"].lower()


def test_rate_limit_key_uses_real_ip_only_from_proxy():
    from types import SimpleNamespace

    from backend.ratelimit import client_ip, limiter

    via_proxy = SimpleNamespace(client=SimpleNamespace(host="172.18.0.5"), headers={"x-real-ip": "203.0.113.9"})
    direct = SimpleNamespace(client=SimpleNamespace(host="8.8.8.8"), headers={"x-real-ip": "1.2.3.4"})
    assert client_ip(via_proxy) == "203.0.113.9"
    assert client_ip(direct) == "8.8.8.8"                 # spoofed header ignored from the internet
    assert limiter._in_memory_fallback_enabled and limiter._swallow_errors


def test_websocket_requires_token(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/market/AAPL") as ws:
            ws.receive_json()
