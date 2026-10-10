"""Desktop sidecar (backend/desktop.py) and DESKTOP_MODE behaviour. Offline; temp data dirs only."""
import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url

import backend.market_data.websocket as ws_mod
from backend import desktop
from backend.config import Settings, settings
from backend.trading.runner import run_forever
from tests.conftest import FakeMarket, make_trending

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "t0k3n-" + "a" * 58
POSIX = os.name != "nt"


# ─── settings.env ─────────────────────────────────────────────────────────────

def test_parse_env_text():
    text = (
        "﻿# a comment\n"
        "\n"
        "TRADING_MODE=alpaca_paper\n"
        "export BOT_UNIVERSE = AAPL,MSFT \n"
        'ALPACA_API_KEY="abc def"  # quoted, then a comment\n'
        "ALERT_WEBHOOK_URL=https://hooks.example/x#frag   # the # inside the URL stays\n"
        "EMPTY=\n"
        "QUOTED='single'\n"
        "not a setting\n"
        "1BAD=x\n"
        'UNTERMINATED="abc\n'
    )
    values, problems = desktop.parse_env_text(text)
    assert values == {
        "TRADING_MODE": "alpaca_paper",
        "BOT_UNIVERSE": "AAPL,MSFT",
        "ALPACA_API_KEY": "abc def",
        "ALERT_WEBHOOK_URL": "https://hooks.example/x#frag",
        "EMPTY": "",
        "QUOTED": "single",
    }
    assert len(problems) == 3 and all("line" in p for p in problems)


def test_settings_template_created_once_and_all_commented(tmp_path):
    path, created = desktop.ensure_settings_file(tmp_path)
    assert created and path == tmp_path / "settings.env"
    assert desktop.parse_env_text(path.read_text()) == ({}, [])    # defaults apply until the user edits it
    for key in ("TRADING_MODE", "ALPACA_API_KEY", "ALLOW_LIVE_TRADING", "ANTHROPIC_API_KEY", "LLM_REVIEW_ENABLED",
                "RESEARCH_LLM_ENABLED", "BOT_UNIVERSE", "BOT_CYCLE_MINUTES", "ALERT_WEBHOOK_URL"):
        assert f"#{key}=" in path.read_text()
    if POSIX:
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("BOT_CYCLE_MINUTES=5\n")
    assert desktop.ensure_settings_file(tmp_path) == (path, False)
    assert path.read_text() == "BOT_CYCLE_MINUTES=5\n"           # never overwritten


def test_settings_env_never_overrides_the_environment(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("TRADING_MODE=alpaca_paper\nBOT_CYCLE_MINUTES=5\nAPP_ENV=development\n"
                    "TRADEBOT_CONTROL_TOKEN=guessable\nDATABASE_URL=sqlite:///elsewhere.db\nJWT_SECRET_KEY=weak\n"
                    "CORS_ORIGINS=https://evil.example\nTRADEBOT_PAIRING_SECRET=AAAAAAAAAAAAAAAAAAAA\n")
    environ = {"TRADING_MODE": "paper"}
    applied, problems = desktop.load_settings_env(path, environ)
    assert applied == ["BOT_CYCLE_MINUTES"]
    assert environ == {"TRADING_MODE": "paper", "BOT_CYCLE_MINUTES": "5"}
    for key in ("APP_ENV", "TRADEBOT_CONTROL_TOKEN", "DATABASE_URL", "JWT_SECRET_KEY", "CORS_ORIGINS",
                "TRADEBOT_PAIRING_SECRET"):
        assert any(key in p for p in problems), key
    assert desktop.load_settings_env(tmp_path / "missing.env", {}) == ([], [])


# ─── secret, environment, data dir ────────────────────────────────────────────

def test_secret_is_generated_once_and_private(tmp_path):
    first = desktop.ensure_secret(tmp_path)
    assert len(first) == 64 and int(first, 16) >= 0
    assert desktop.ensure_secret(tmp_path) == first               # persisted across starts
    path = tmp_path / "secret.key"
    if POSIX:
        assert path.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]   # atomic write left no temp file
    path.write_text("short\n")
    replaced = desktop.ensure_secret(tmp_path)
    assert replaced != "short" and len(replaced) == 64


def test_configure_environment(tmp_path):
    data_dir = tmp_path / "AI Trading Bot"          # spaces, as on macOS/Windows
    data_dir.mkdir()
    env = {"APP_ENV": "development", "DESKTOP_MODE": "false", "BOT_UNIVERSE": "SPY"}
    desktop.configure_environment(data_dir, 47822, tmp_path / "out", env)
    assert env["DESKTOP_MODE"] == "true" and env["APP_ENV"] == "desktop" and env["DB_AUTO_CREATE"] == "false"
    assert env["BOT_UNIVERSE"] == "SPY"                            # defaults never override
    assert env["JWT_SECRET_KEY"] == (data_dir / "secret.key").read_text().strip()
    assert make_url(env["DATABASE_URL"]).database == (data_dir / "trading.db").as_posix()
    assert env["DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    assert env["REDIS_URL"] == ""
    assert env["CORS_ORIGINS"].split(",") == ["http://127.0.0.1:47822", "http://localhost:47822"]
    assert env["TRADEBOT_DATA_DIR"] == str(data_dir) and env["TRADEBOT_STATIC_DIR"] == str(tmp_path / "out")
    assert env["TRADEBOT_PAIRING_SECRET"] == (data_dir / "pairing.key").read_text().strip()

    # A database / secret exported for another project never redirects the desktop app.
    stray = {"JWT_SECRET_KEY": "x" * 40, "DATABASE_URL": "postgresql://elsewhere/db", "REDIS_URL": "redis://r:6379",
             "TRUSTED_PROXY_CIDRS": "0.0.0.0/0", "CORS_ORIGINS": "https://mine.example"}
    desktop.configure_environment(data_dir, 47821, None, stray)
    assert stray["JWT_SECRET_KEY"] == env["JWT_SECRET_KEY"]
    assert stray["DATABASE_URL"] == env["DATABASE_URL"] and stray["REDIS_URL"] == ""
    # Only the app's own origin may read API responses: the environment cannot widen CORS either.
    assert stray["TRUSTED_PROXY_CIDRS"] == "" and stray["CORS_ORIGINS"] == "http://127.0.0.1:47821,http://localhost:47821"
    assert stray["TRADEBOT_PAIRING_SECRET"] == env["TRADEBOT_PAIRING_SECRET"]       # one code per installation
    assert "TRADEBOT_STATIC_DIR" not in stray


def test_pairing_code_is_generated_once_and_private(tmp_path):
    code = desktop.ensure_pairing_secret(tmp_path)
    assert len(code) == desktop.PAIRING_LENGTH == 20 and set(code) <= set(desktop.PAIRING_ALPHABET)
    assert desktop.ensure_pairing_secret(tmp_path) == code
    if POSIX:
        assert (tmp_path / "pairing.key").stat().st_mode & 0o777 == 0o600
    (tmp_path / "pairing.key").write_text("not-a-code\n")
    replaced = desktop.ensure_pairing_secret(tmp_path)
    assert replaced != code and len(replaced) == 20


def test_desktop_env_is_not_development():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, APP_ENV="desktop", JWT_SECRET_KEY="too-short")
    s = Settings(_env_file=None, APP_ENV="desktop", DESKTOP_MODE=True, JWT_SECRET_KEY="f" * 64)
    assert not s.is_development and s.DESKTOP_MODE


def test_default_data_dir_per_platform(tmp_path):
    home = tmp_path / "home"
    win = desktop.default_data_dir("win32", {"APPDATA": str(tmp_path / "Roaming")}, home)
    assert win == tmp_path / "Roaming" / "AI Trading Bot"
    assert desktop.default_data_dir("darwin", {}, home) == home / "Library" / "Application Support" / "AI Trading Bot"
    assert desktop.default_data_dir("linux", {}, home) == home / ".local" / "share" / "ai-trading-bot"
    assert desktop.resolve_data_dir(None, {"TRADEBOT_DATA_DIR": str(tmp_path / "d")}) == (tmp_path / "d").resolve()
    assert desktop.resolve_data_dir(str(tmp_path / "cli"), {"TRADEBOT_DATA_DIR": "x"}) == (tmp_path / "cli").resolve()


# ─── port, instance lock ──────────────────────────────────────────────────────

def test_port_selection_skips_busy_ports():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        sock = desktop.bind_port(busy, fallbacks=20)
        try:
            assert busy < sock.getsockname()[1] <= busy + 20
        finally:
            sock.close()
        with pytest.raises(desktop.StartupError):
            desktop.bind_port(busy, fallbacks=0)
    finally:
        blocker.close()
    anywhere = desktop.bind_port(0)
    assert anywhere.getsockname()[1] > 0
    anywhere.close()
    assert desktop.parse_port(None) == desktop.DEFAULT_PORT == 47821
    assert desktop.parse_port(" 0 ") == 0
    for bad in ("abc", "70000", "-1"):
        with pytest.raises(desktop.StartupError):
            desktop.parse_port(bad)


class _FakeWindowsSockets:
    """Windows binding without Windows: exclusive binds of `held` fail `failures` times (the last run's TIME_WAIT)."""

    def __init__(self, held, failures, listening=False):
        self.held, self.failures, self.listening = held, failures, listening
        self.calls = []

    def open(self, host, port, mode):
        self.calls.append((port, mode))
        if port == self.held and mode == "exclusive" and self.failures > 0:
            self.failures -= 1
            raise OSError(10048, "WSAEADDRINUSE")
        return ("socket", port, mode)

    def install(self, monkeypatch):
        monkeypatch.setattr(desktop, "_WINDOWS", True)
        monkeypatch.setattr(desktop, "_open_listener", self.open)
        monkeypatch.setattr(desktop, "_someone_listens", lambda host, port: self.listening and port == self.held)
        monkeypatch.setattr(desktop.time, "sleep", lambda s: None)


def test_windows_restart_keeps_the_port_despite_time_wait(monkeypatch):
    # The previous run's connections block the exclusive bind for a moment: retried, same port.
    fake = _FakeWindowsSockets(held=47821, failures=3)
    fake.install(monkeypatch)
    assert desktop.bind_port(47821) == ("socket", 47821, "exclusive")
    assert [c for c in fake.calls if c[0] != 47821] == []

    # Still blocked after the retries: bound without SO_EXCLUSIVEADDRUSE, still the same port.
    fake = _FakeWindowsSockets(held=47821, failures=10**6)
    fake.install(monkeypatch)
    assert desktop.bind_port(47821, retry_seconds=0.05) == ("socket", 47821, "plain")

    # Somebody really listens there: never shared, the next port is used at once.
    fake = _FakeWindowsSockets(held=47821, failures=10**6, listening=True)
    fake.install(monkeypatch)
    assert desktop.bind_port(47821) == ("socket", 47822, "exclusive")
    assert ("plain" not in {mode for _, mode in fake.calls})


def test_one_instance_per_data_dir(tmp_path):
    first, second = desktop.InstanceLock(tmp_path / "tradebot.lock"), desktop.InstanceLock(tmp_path / "tradebot.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


# ─── DESKTOP_MODE app: auth, trusted hosts, control API, static frontend ──────

@pytest.fixture
def static_dir(tmp_path):
    out = tmp_path / "out"
    (out / "dashboard").mkdir(parents=True)
    (out / "_next" / "static").mkdir(parents=True)
    (out / "index.html").write_text("<html>ROOT INDEX</html>")
    (out / "dashboard" / "index.html").write_text("<html>DASHBOARD</html>")
    (out / "404.html").write_text("<html>NOT FOUND PAGE</html>")
    (out / "_next" / "static" / "app.js").write_text("console.log('app')")
    return out


@pytest.fixture
def desktop_mode(monkeypatch, tmp_path, static_dir):
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "TRADEBOT_CONTROL_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "TRADEBOT_STATIC_DIR", str(static_dir))
    monkeypatch.setattr(settings, "TRADEBOT_DATA_DIR", str(tmp_path))


@pytest.fixture
def desktop_app(db_tables, desktop_mode):
    from backend.main import create_app

    app = create_app()
    stops = []
    app.state.request_shutdown = stops.append     # injected: never stops the test process
    app.state.stops = stops
    return app


@pytest.fixture
def client(desktop_app):
    with TestClient(desktop_app, base_url="http://127.0.0.1:47821") as c:
        yield c


APP_WINDOW = {"X-Desktop-Token": TOKEN}      # what the Electron shell adds to its window's /auth/register


def _register(c, name, headers=APP_WINDOW):
    return c.post("/auth/register", json={"email": f"{name}@example.com", "username": name,
                                          "password": "correct-horse-battery"}, headers=headers)


def test_desktop_first_account_is_admin_then_registration_closes(client, monkeypatch):
    monkeypatch.setattr(settings, "REGISTRATION_OPEN", False)      # ignored in desktop mode
    first = _register(client, "owner")
    assert first.status_code == 201 and first.json()["is_superuser"] is True
    second = _register(client, "intruder")
    assert second.status_code == 403
    monkeypatch.setattr(settings, "REGISTRATION_OPEN", True)
    assert _register(client, "intruder").status_code == 403         # closed regardless of REGISTRATION_OPEN
    tok = client.post("/auth/login", json={"email": "owner@example.com", "password": "correct-horse-battery"}).json()
    status = client.get("/api/bot/status", headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert status.status_code == 200 and status.json()["enabled"] is False     # the bot starts paused


def _users(db_tables):
    import sqlite3

    url = make_url(settings.DATABASE_URL)
    with sqlite3.connect(url.database) as db:
        return db.execute("select email, is_superuser from users").fetchall()


def test_first_account_can_only_be_created_from_the_app_window(client):
    """SEC-1: before the owner registers, nobody else on the machine may claim the administrator account."""
    for headers in ({}, {"X-Desktop-Token": "guess"}, {"X-Desktop-Token": TOKEN[:-1]}):
        r = _register(client, "intruder", headers=headers)
        assert r.status_code == 403 and r.json()["detail"] == "Create the first account in the AI Trading Bot app window"
    # A web page's no-cors "simple" request: a body without Content-Type, its own Origin.
    body = json.dumps({"email": "attacker@evilcorp.com", "username": "attacker", "password": "AttackerPassw0rd!!"})
    blob = client.post("/auth/register", content=body, headers={"Content-Type": "", "Origin": "http://localhost:8123",
                                                                 "Sec-Fetch-Site": "cross-site", **APP_WINDOW})
    assert blob.status_code == 403
    same_origin_no_type = client.post("/auth/register", content=body, headers={"Content-Type": "", **APP_WINDOW})
    assert same_origin_no_type.status_code == 415
    owner = _register(client, "owner")                                  # the owner, from the app window
    assert owner.status_code == 201 and owner.json()["is_superuser"] is True
    assert _register(client, "late").status_code == 403                 # then closed for everyone


def test_first_account_without_a_shell_is_open_to_local_callers(db_tables, desktop_mode, monkeypatch):
    """A sidecar started by hand from source (no control token) keeps the old first-come behaviour."""
    from backend.main import create_app

    monkeypatch.setattr(settings, "TRADEBOT_CONTROL_TOKEN", "")
    with TestClient(create_app(), base_url="http://127.0.0.1:47821") as c:
        first = _register(c, "dev", headers={})
        assert first.status_code == 201 and first.json()["is_superuser"] is True
        assert _register(c, "second", headers={}).status_code == 403


def test_cross_site_requests_are_refused_before_anything_runs(client):
    """SEC-3: CORS only hides responses; in desktop mode other web sites' requests must not run at all."""
    assert _register(client, "owner").status_code == 201
    login = {"email": "owner@example.com", "password": "correct-horse-battery"}
    evil = [{"Origin": "https://evil.example"}, {"Origin": "http://127.0.0.1:8123"}, {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Origin": "http://localhost:8123", "Sec-Fetch-Site": "same-site"}]
    for headers in evil:
        assert client.post("/auth/login", json=login, headers=headers).status_code == 403, headers
        assert client.get("/api/bot/status", headers=headers).status_code == 403, headers
    # Allowed: the app's own origin, the Chrome extension, clients that send neither header.
    own = client.post("/auth/login", json=login, headers={"Origin": "http://127.0.0.1:47821",
                                                           "Sec-Fetch-Site": "same-origin"})
    assert own.status_code == 200
    ext = client.post("/auth/login", json=login, headers={"Origin": "chrome-extension://abcdefghijklmnop",
                                                           "Sec-Fetch-Site": "none"})
    assert ext.status_code == 200
    assert client.post("/auth/login", json=login).status_code == 200
    # A link from another site to the dashboard still opens it (static pages are not API paths).
    page = client.get("/dashboard/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    assert page.status_code == 200 and "DASHBOARD" in page.text
    # Bodies must be JSON: text/plain and no Content-Type (no-cors fetch) are refused.
    for content_type in ("", "text/plain", "application/x-www-form-urlencoded"):
        r = client.post("/auth/login", content=json.dumps(login), headers={"Content-Type": content_type})
        assert r.status_code == 415, content_type
    token = own.json()["access_token"]
    started = client.post("/api/bot/start", headers={"Authorization": f"Bearer {token}"})   # no body: fine
    assert started.status_code == 200


def test_local_floods_cannot_lock_the_owner_out(client):
    """SEC-3: every desktop client is 127.0.0.1, so per-IP buckets would be one bucket anybody can exhaust."""
    assert _register(client, "owner").status_code == 201
    good = {"email": "owner@example.com", "password": "correct-horse-battery"}
    for i in range(15):                                    # another local process spraying logins
        assert client.post("/auth/login", json={"email": f"x{i}@example.com", "password": "nope"}).status_code == 401
    for _ in range(35):                                    # ...and bogus refresh tokens
        assert client.post("/auth/refresh", json={"refresh_token": "x"}).status_code == 401
    pair = client.post("/auth/login", json=good)
    assert pair.status_code == 200, pair.text
    assert client.post("/auth/refresh", json={"refresh_token": pair.json()["refresh_token"]}).status_code == 200
    # Unauthenticated floods use up the shared bucket; the owner's requests count against their account.
    import backend.ratelimit as rl
    auth = {"Authorization": f"Bearer {pair.json()['access_token']}"}
    assert rl.rate_limit_key(_FakeRequest(auth)).startswith("user:")
    assert rl.rate_limit_key(_FakeRequest({"Authorization": "Bearer forged.token.value"})) == "127.0.0.1"
    # The owner's own sign-ins (app window, extension) never use up the allowance...
    assert all(client.post("/auth/login", json=good).status_code == 200 for _ in range(12))
    # ...guessing the owner's password is still limited, per account (failed attempts).
    codes = [client.post("/auth/login", json={**good, "password": "wrong-guess"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert client.post("/auth/login", json=good).status_code == 429       # no oracle while locked
    assert client.post("/auth/login", json={"email": "other@example.com", "password": "x"}).status_code == 401


class _FakeRequest:
    def __init__(self, headers):
        from starlette.datastructures import Headers

        self.headers = Headers(headers)
        self.client = type("C", (), {"host": "127.0.0.1"})()


def test_api_docs_are_not_served_in_desktop_mode(client):
    """SEC-5: Swagger UI / ReDoc would run CDN scripts on the origin that holds the dashboard's tokens."""
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = client.get(path)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, path
        assert "cdn.jsdelivr.net" not in r.text


def test_extension_pairing_proof(client, desktop_app, monkeypatch):
    """SEC-2: the extension checks the server knows this installation's pairing code, bound to its own URL."""
    import hashlib
    import hmac

    secret = "ABCDEFGHIJKLMNOPQRST"
    monkeypatch.setattr(settings, "TRADEBOT_PAIRING_SECRET", secret)
    from backend.main import create_app

    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:47821") as c:
        nonce = "0123456789abcdef" * 4
        assert c.get(f"/api/desktop/pair?nonce={nonce}").status_code == 503      # URL unknown: no answer
        app.state.desktop_url = "http://127.0.0.1:47822"                          # the port it really bound
        r = c.get(f"/api/desktop/pair?nonce={nonce}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        expected = hmac.new(secret.encode(), f"tradebot-pair-v1\nhttp://127.0.0.1:47822\n{nonce}".encode(),
                            hashlib.sha256).hexdigest()
        assert body == {"v": 1, "server": "http://127.0.0.1:47822", "proof": expected}
        # A squatter relaying the challenge from 47821 gets a proof for 47822, not for the URL it serves.
        for_47821 = hmac.new(secret.encode(), f"tradebot-pair-v1\nhttp://127.0.0.1:47821\n{nonce}".encode(),
                             hashlib.sha256).hexdigest()
        assert body["proof"] != for_47821
        for bad in ("short", "zz" * 20, "a" * 200):
            assert c.get(f"/api/desktop/pair?nonce={bad}").status_code == 422
    # Without a pairing secret (or outside desktop mode) the endpoint does not exist.
    assert client.get(f"/api/desktop/pair?nonce={'a' * 32}").status_code == 404


def test_trusted_host_rejects_foreign_host_header(desktop_app):
    assert TestClient(desktop_app, base_url="http://evil.example.com").get("/health").status_code == 400
    # DNS rebinding: attacker's name resolving to 127.0.0.1 still carries the attacker's Host header
    assert TestClient(desktop_app, base_url="http://rebind.attacker.test:47821").get("/").status_code == 400
    assert TestClient(desktop_app, base_url="http://localhost:47821").get("/health").status_code == 200
    assert TestClient(desktop_app, base_url="http://127.0.0.1:47821").get("/health").status_code == 200


def test_control_api_requires_token(client, desktop_app):
    assert client.get("/api/desktop/info").status_code == 403
    assert client.get("/api/desktop/info", headers={"X-Desktop-Token": "wrong"}).status_code == 403
    assert client.post("/api/desktop/shutdown").status_code == 403
    assert client.post("/api/desktop/shutdown", headers={"X-Desktop-Token": TOKEN[:-1]}).status_code == 403
    assert desktop_app.state.stops == []

    info = client.get("/api/desktop/info", headers={"X-Desktop-Token": TOKEN})
    assert info.status_code == 200
    body = info.json()
    assert set(body) == {"version", "url", "data_dir", "trading_mode", "bot_enabled", "halted", "open_positions",
                         "last_cycle_at"}
    assert body["trading_mode"] == "paper" and body["bot_enabled"] is False and body["halted"] is False
    assert body["open_positions"] == 0 and body["last_cycle_at"] is None

    r = client.post("/api/desktop/shutdown", headers={"X-Desktop-Token": TOKEN})
    assert r.status_code == 202 and r.json() == {"stopping": True, "deadline_seconds": None}
    assert desktop_app.state.stops == ["control API"]


def test_control_api_absent_without_token(db_tables, desktop_mode, monkeypatch):
    from backend.main import create_app

    monkeypatch.setattr(settings, "TRADEBOT_CONTROL_TOKEN", "")
    with TestClient(create_app(), base_url="http://127.0.0.1:47821") as c:
        r = c.get("/api/desktop/info", headers={"X-Desktop-Token": ""})
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
        assert c.post("/api/desktop/shutdown").status_code in (404, 405)


def test_static_frontend_served_and_api_wins(client):
    root = client.get("/")
    assert root.status_code == 200 and "ROOT INDEX" in root.text            # not the API's JSON root
    redirect = client.get("/dashboard", follow_redirects=False)
    assert redirect.status_code in (307, 308) and redirect.headers["location"].endswith("/dashboard/")
    assert "DASHBOARD" in client.get("/dashboard/").text
    assert client.get("/_next/static/app.js").text == "console.log('app')"
    missing = client.get("/no/such/page/")
    assert missing.status_code == 404 and "NOT FOUND PAGE" in missing.text
    api_missing = client.get("/api/no-such-endpoint")
    assert api_missing.status_code == 404 and api_missing.json() == {"detail": "Not Found"}
    assert client.get("/health").json()["status"] == "healthy"
    assert client.get("/api/market/quote/bad$sym").status_code == 404 and "detail" in client.get(
        "/api/market/quote/bad$sym").json()
    assert client.post("/").status_code == 405
    # Static assets are not rate limited (every desktop request comes from 127.0.0.1).
    assert all(client.get("/_next/static/app.js").status_code == 200 for _ in range(150))


def test_static_frontend_cache_headers(client, static_dir):
    """After an upgrade the HTML must not come from Chromium's disk cache (old UI, missing chunks)."""
    (static_dir / "dashboard" / "index.txt").write_text("RSC payload")
    for path in ("/", "/dashboard/", "/dashboard/index.txt", "/no/such/page/"):
        r = client.get(path)
        assert r.headers["cache-control"] == "no-cache", path
        assert "etag" in r.headers or r.status_code == 404, path
    asset = client.get("/_next/static/app.js")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    revalidated = client.get("/dashboard/", headers={"If-None-Match": client.get("/dashboard/").headers["etag"]})
    assert revalidated.status_code == 304 and revalidated.headers["cache-control"] == "no-cache"


def test_desktop_websocket_streams_through_same_origin(client, monkeypatch):
    monkeypatch.setattr(ws_mod, "market_data_service", FakeMarket({"AAPL": make_trending(1)}))
    assert _register(client, "owner").status_code == 201
    tok = client.post("/auth/login", json={"email": "owner@example.com", "password": "correct-horse-battery"}).json()
    # TestClient.websocket_connect resolves relative URLs against ws://testserver,
    # which TrustedHostMiddleware rejects: use the sidecar's own origin.
    url = f"ws://127.0.0.1:47821/ws/market/AAPL?token={tok['access_token']}"
    with client.websocket_connect(url, headers={"Origin": "http://127.0.0.1:47821"}) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "quote" and msg["data"]["symbol"] == "AAPL"
        ws.send_json({"type": "unsubscribe"})
    # Another web site cannot open the stream (cross-site WebSocket hijacking).
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_nothing_changes_outside_desktop_mode(db_tables, static_dir, monkeypatch):
    from backend.main import create_app

    monkeypatch.setattr(settings, "TRADEBOT_STATIC_DIR", str(static_dir))
    monkeypatch.setattr(settings, "TRADEBOT_CONTROL_TOKEN", TOKEN)
    assert settings.DESKTOP_MODE is False
    c = TestClient(create_app())                                    # Host: testserver is fine here
    assert c.get("/").json()["message"] == "AI Trading Platform API"
    assert c.get("/api/desktop/info", headers={"X-Desktop-Token": TOKEN}).status_code == 404


# ─── bot loop and graceful shutdown ───────────────────────────────────────────

async def test_run_forever_finishes_the_cycle_before_stopping():
    stop = asyncio.Event()

    class Agent:
        calls = finished = 0

        async def run_cycle(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")            # a failing cycle never kills the loop
            stop.set()                                # a stop request arrives mid-cycle...
            await asyncio.sleep(0.05)
            self.finished += 1                        # ...and the cycle still completes
            return {"cycle_id": "c", "status": "ok"}

    agent = Agent()
    await asyncio.wait_for(run_forever(stop, agent, interval=0.01), 5)
    assert agent.calls == 2 and agent.finished == 1


async def _serve_in_process(app, tmp_path, bot, bot_stop_timeout=150.0):
    sock = desktop.bind_port(0)
    port = sock.getsockname()[1]
    ctx = desktop.LaunchContext(data_dir=tmp_path, port=port, url=f"http://127.0.0.1:{port}", version="9.9.9",
                                install_signals=False, bot_stop_timeout=bot_stop_timeout)
    task = asyncio.create_task(desktop.serve_app(app, sock, ctx, bot_runner=bot))
    for _ in range(500):
        if (tmp_path / "server.json").exists() or task.done():
            break
        await asyncio.sleep(0.02)
    assert not task.done(), task.exception()
    return task, ctx


async def test_shutdown_waits_for_the_running_cycle(db_tables, desktop_mode, tmp_path, capsys):
    from backend.main import create_app

    events, cycle_running = [], asyncio.Event()

    async def bot(stop):
        while not stop.is_set():
            events.append("cycle start")
            cycle_running.set()
            await asyncio.sleep(0.5)
            events.append("cycle end")
            try:
                await asyncio.wait_for(stop.wait(), 3600)
            except asyncio.TimeoutError:
                pass
        events.append("loop exit")

    app = create_app()
    task, ctx = await _serve_in_process(app, tmp_path, bot)
    ready = [line for line in capsys.readouterr().out.splitlines() if line.startswith("TRADEBOT_READY ")]
    assert json.loads(ready[0].split(" ", 1)[1]) == {"url": ctx.url, "port": ctx.port, "data_dir": str(tmp_path),
                                                      "version": "9.9.9"}
    assert json.loads((tmp_path / "server.json").read_text()) == {"url": ctx.url, "port": ctx.port, "pid": os.getpid()}
    await asyncio.wait_for(cycle_running.wait(), 5)
    async with httpx.AsyncClient(base_url=ctx.url) as http:
        assert (await http.get("/health")).status_code == 200
        assert (await http.post("/api/desktop/shutdown")).status_code == 403
        r = await http.post("/api/desktop/shutdown", headers={"X-Desktop-Token": TOKEN})
        assert r.status_code == 202 and r.json() == {"stopping": True, "deadline_seconds": 210}
    assert await asyncio.wait_for(task, 30) == 0
    assert events == ["cycle start", "cycle end", "loop exit"]       # the in-flight cycle completed
    assert not (tmp_path / "server.json").exists()


async def test_shutdown_cancels_a_cycle_that_overruns_the_limit(db_tables, desktop_mode, tmp_path):
    from backend.main import create_app

    events = []

    async def stuck_bot(stop):
        events.append("cycle start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    app = create_app()
    task, ctx = await _serve_in_process(app, tmp_path, stuck_bot, bot_stop_timeout=0.3)
    app.state.request_shutdown("test")
    assert await asyncio.wait_for(task, 30) == desktop.EXIT_CUT_OFF        # not a clean stop: say so
    assert events == ["cycle start", "cancelled"]


async def test_urgent_shutdown_does_not_wait_for_the_cycle(db_tables, desktop_mode, tmp_path):
    """Windows session end: the shell asks again with urgent=true, and the stop no longer waits 150 s."""
    from backend.main import create_app

    events = []

    async def stuck_bot(stop):
        events.append("cycle start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    app = create_app()
    task, ctx = await _serve_in_process(app, tmp_path, stuck_bot)          # the normal 150 s limit
    async with httpx.AsyncClient(base_url=ctx.url) as http:
        first = await http.post("/api/desktop/shutdown", headers={"X-Desktop-Token": TOKEN})
        assert first.status_code == 202
        await asyncio.sleep(0.3)
        assert not task.done()                                              # waiting for the cycle
        urgent = await http.post("/api/desktop/shutdown?urgent=true", headers={"X-Desktop-Token": TOKEN})
        assert urgent.status_code == 202
    assert await asyncio.wait_for(task, 30) == desktop.EXIT_CUT_OFF
    assert events == ["cycle start", "cancelled"]


def _inflight_app(events, seconds):
    """A stand-in for the real routes: POST /api/bot/run-once and /flatten doing `seconds` of trading work."""
    from fastapi import FastAPI

    app = FastAPI()

    async def work(name):
        events.append(f"{name} start")
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            events.append(f"{name} CANCELLED")
            raise
        events.append(f"{name} finished")
        return {"status": "ok"}

    async def run_once():
        return await work("run-once")

    async def flatten():
        return await work("flatten")

    app.add_api_route("/api/bot/run-once", run_once, methods=["POST"])
    app.add_api_route("/api/bot/flatten", flatten, methods=["POST"])
    return app


async def _idle_bot(stop):
    await stop.wait()


async def _until(condition, timeout=20.0):
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def test_shutdown_waits_for_bot_operations_started_through_the_api(tmp_path):
    """SEC-4: a run-once / flatten in progress at quit finishes; new ones are refused meanwhile."""
    events = []
    app = _inflight_app(events, seconds=7)              # longer than the HTTP server's own 5 s grace
    task, ctx = await _serve_in_process(app, tmp_path, _idle_bot)
    async with httpx.AsyncClient(base_url=ctx.url, timeout=60) as http:
        running = asyncio.create_task(http.post("/api/bot/run-once"))
        await _until(lambda: "run-once start" in events)
        app.state.request_shutdown("quit")
        await asyncio.sleep(0.2)
        refused = await http.post("/api/bot/run-once")
        assert refused.status_code == 503 and "shutting down" in refused.json()["detail"]
        flatten = await http.post("/api/bot/flatten")     # the panic button still works while stopping
        assert flatten.status_code == 200
        assert (await running).status_code == 200
    assert await asyncio.wait_for(task, 60) == 0
    assert events == ["run-once start", "flatten start", "run-once finished", "flatten finished"]
    async with httpx.AsyncClient(base_url=ctx.url, timeout=5) as http:      # gone once stopped
        with pytest.raises(httpx.HTTPError):
            await http.post("/api/bot/flatten")


async def test_bot_operation_overrunning_the_stop_limit_is_reported(tmp_path):
    events = []
    app = _inflight_app(events, seconds=3600)
    task, ctx = await _serve_in_process(app, tmp_path, _idle_bot, bot_stop_timeout=0.5)
    async with httpx.AsyncClient(base_url=ctx.url, timeout=60) as http:
        running = asyncio.create_task(http.post("/api/bot/run-once"))
        await _until(lambda: "run-once start" in events)
        app.state.request_shutdown("quit")
        assert await asyncio.wait_for(task, 60) == desktop.EXIT_CUT_OFF
        with contextlib.suppress(httpx.HTTPError):
            await running
    assert events == ["run-once start", "run-once CANCELLED"]


def test_shutdown_deadline_covers_the_worst_case():
    worst = (desktop.BOT_STOP_TIMEOUT + desktop.BOT_CANCEL_WAIT + desktop.HTTP_STOP_WAIT + desktop.TASK_CANCEL_WAIT
             + desktop.EXECUTOR_GRACE)
    assert desktop.shutdown_deadline() >= worst and desktop.shutdown_deadline() == 210
    assert desktop.shutdown_deadline(0.3) == 61


# ─── the real process (python -m backend.desktop) ─────────────────────────────

def _sidecar_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("APP_ENV", "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "ML_SERVICE_URL",
                        "LLM_REVIEW_ENABLED", "TRADING_MODE", "BOT_UNIVERSE", "DESKTOP_MODE")
           and not k.startswith("TRADEBOT_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
    env["HOME"] = str(tmp_path / "home")                     # never touch the real home directory
    env["APPDATA"] = str(tmp_path / "home" / "AppData")
    env.update(extra)
    return env


def _read_protocol_line(proc, timeout=120):
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: lines.put(proc.stdout.readline().decode()), daemon=True).start()
    return lines.get(timeout=timeout).strip()


def test_sidecar_process_ready_then_graceful_exit(tmp_path):
    data = tmp_path / "data dir"
    env = _sidecar_env(tmp_path, TRADEBOT_DATA_DIR=str(data), TRADEBOT_CONTROL_TOKEN=TOKEN, TRADEBOT_PORT="0")
    cmd = [sys.executable, "-m", "backend.desktop", "--no-bot"]
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        line = _read_protocol_line(proc)
        assert line.startswith("TRADEBOT_READY "), line
        ready = json.loads(line.split(" ", 1)[1])
        assert ready["data_dir"] == str(data.resolve()) and ready["url"] == f"http://127.0.0.1:{ready['port']}"
        with urllib.request.urlopen(ready["url"] + "/health", timeout=30) as r:
            assert json.loads(r.read())["status"] == "healthy"
        for name in ("trading.db", "secret.key", "settings.env", "server.json", "logs/backend.log"):
            assert (data / name).exists(), name

        # A second instance on the same data directory refuses to start.
        twin = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, timeout=120)
        assert twin.returncode != 0
        assert twin.stdout.decode().startswith("TRADEBOT_ERROR ") and "already running" in twin.stdout.decode()

        req = urllib.request.Request(ready["url"] + "/api/desktop/shutdown", method="POST",
                                     headers={"X-Desktop-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 202
        assert proc.wait(timeout=60) == 0
        assert not (data / "server.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_sidecar_reports_fatal_startup_errors(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.env").write_text("TRADING_MODE=alpaca_live\n")      # without ALLOW_LIVE_TRADING
    r = subprocess.run([sys.executable, "-m", "backend.desktop", "--no-bot", "--port", "0", "--data-dir", str(data)],
                       cwd=REPO_ROOT, env=_sidecar_env(tmp_path), capture_output=True, timeout=120)
    out = r.stdout.decode().strip()
    assert r.returncode != 0
    assert out.startswith("TRADEBOT_ERROR ") and "ALLOW_LIVE_TRADING" in out and len(out.splitlines()) == 1
    assert (data / "secret.key").read_text().strip() not in out                # no secrets in the message


def test_recovery_commands(tmp_path):
    data = tmp_path / "data"
    env = _sidecar_env(tmp_path, ADMIN_PASSWORD="correct-horse-battery-staple")
    base = [sys.executable, "-m", "backend.desktop", "--data-dir", str(data)]
    r = subprocess.run(base + ["create-admin", "--email", "Boss@Example.com", "--username", "boss"],
                       cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "created" in r.stdout
    with sqlite3.connect(data / "trading.db") as db:      # migrated with Alembic inside the data dir
        assert db.execute("select email, is_superuser, token_version from users").fetchall() == [
            ("boss@example.com", 1, 0)]
        assert db.execute("select version_num from alembic_version").fetchone()[0]
    # --data-dir is also accepted after the subcommand
    r = subprocess.run([sys.executable, "-m", "backend.desktop", "revoke-tokens", "--email", "boss@example.com",
                        "--data-dir", str(data)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "revoked" in r.stdout, r.stderr
    with sqlite3.connect(data / "trading.db") as db:
        assert db.execute("select token_version from users").fetchone()[0] == 1
    r = subprocess.run(base + ["revoke-tokens", "--email", "nobody@example.com"], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and "No such user" in r.stdout


# ─── logs, database backups, version, packaging ──────────────────────────────

def test_tokens_are_redacted_from_logs():
    from backend.logging_utils import RedactTokens, redact

    # uvicorn's WebSocket handshake line (logger uvicorn.error; the client is a tuple)
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" %d',
                               (("127.0.0.1", 5), "/ws/market/AAPL?token=eyJhbGc.payload.sig&x=1", 403), None)
    assert RedactTokens().filter(record)
    assert record.getMessage() == "('127.0.0.1', 5) - \"WebSocket /ws/market/AAPL?token=[redacted]&x=1\" 403"
    plain = logging.LogRecord("x", logging.INFO, __file__, 1, "cycle %s ok", ("abc",), None)
    assert RedactTokens().filter(plain) and plain.getMessage() == "cycle abc ok"
    assert redact("/a?access_token=A&refresh_token=B&apiKey=C&api_key=D&symbol=AAPL") == (
        "/a?access_token=[redacted]&refresh_token=[redacted]&apiKey=[redacted]&api_key=[redacted]&symbol=AAPL")
    assert redact(redact("?token=abc")) == "?token=[redacted]"                    # idempotent
    assert desktop.RedactTokens is RedactTokens


def test_redaction_keeps_uvicorn_access_log_records_formattable():
    """uvicorn's AccessFormatter unpacks record.args: redaction must keep the five arguments."""
    from uvicorn.logging import AccessFormatter

    from backend.logging_utils import RedactTokens

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", "/ws/market/AAPL?token=eyJ.secret.sig", "1.1", 101), None)
    assert RedactTokens().filter(record)
    line = AccessFormatter("%(client_addr)s %(request_line)s %(status_code)s", use_colors=False).format(record)
    assert line == "127.0.0.1:5000 GET /ws/market/AAPL?token=[redacted] HTTP/1.1 101 Switching Protocols"

    class Url:                       # an httpx.URL-like argument: only its str() shows the secret
        def __str__(self):
            return "https://finnhub.io/api/v1/quote?symbol=AAPL&token=finnhub-key"

    out = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("GET", Url()), None)
    assert RedactTokens().filter(out)
    assert out.getMessage() == "HTTP Request: GET https://finnhub.io/api/v1/quote?symbol=AAPL&token=[redacted]"


def test_redaction_is_installed_on_the_url_loggers(caplog):
    import backend.main  # noqa: F401  (installs the filters for the normal server too)
    from backend.logging_utils import RedactTokens, URL_LOGGERS, install_token_redaction

    install_token_redaction()                                   # idempotent
    for name in URL_LOGGERS:
        assert sum(isinstance(f, RedactTokens) for f in logging.getLogger(name).filters) == 1, name
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', ("127.0.0.1", 1),
                                                "/ws/market/AAPL?token=eyJ.secret.sig")
    assert "eyJ.secret.sig" not in caplog.text and "token=[redacted]" in caplog.text


def test_database_is_backed_up_before_a_schema_upgrade(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "trading.db"
    monkeypatch.setattr(settings, "DATABASE_URL", desktop.sqlite_url(db_file))     # what env.py migrates
    cfg = Config()
    cfg.set_main_option("script_location", str(desktop.migrations_dir()))
    command.upgrade(cfg, "0003")                    # a database written by an older app version
    with sqlite3.connect(db_file) as db:
        db.execute("INSERT INTO users (email, username, hashed_password, is_active, is_superuser, token_version) "
                   "VALUES ('a@example.com', 'a', 'x', 1, 1, 0)")
    backups = tmp_path / "backups"

    desktop.run_migrations(backup_dir=backups)
    saved = list(backups.glob("trading-*.db"))
    assert len(saved) == 1 and "0003-to-" in saved[0].name
    with sqlite3.connect(saved[0]) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"
        assert db.execute("SELECT email FROM users").fetchall() == [("a@example.com",)]
    if POSIX:
        assert saved[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(db_file) as db:
        head = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert head != "0003"

    desktop.run_migrations(backup_dir=backups)          # already at head: nothing to back up
    assert len(list(backups.glob("trading-*.db"))) == 1
    for _ in range(7):
        desktop.backup_database(db_file, backups, "manual", keep=3)
    assert len(list(backups.glob("trading-*.db"))) == 3

    fresh = tmp_path / "fresh.db"                        # a brand-new database needs no backup
    monkeypatch.setattr(settings, "DATABASE_URL", desktop.sqlite_url(fresh))
    desktop.run_migrations(backup_dir=tmp_path / "fresh-backups")
    assert fresh.exists() and not (tmp_path / "fresh-backups").exists()


def test_app_version_prefers_the_desktop_package(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "bundle_dir", lambda: tmp_path)
    assert desktop.app_version(settings) == settings.APP_VERSION
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "package.json").write_text(json.dumps({"name": "x", "version": "1.4.2"}))
    assert desktop.app_version(settings) == "1.4.2"
    (tmp_path / desktop.VERSION_FILE).write_text("1.5.0\n")         # stamped into the bundle
    assert desktop.app_version(settings) == "1.5.0"


def _requirements(path):
    """{normalised name: version spec} of a requirements file (extras dropped)."""
    reqs = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            m = re.match(r"([A-Za-z0-9_.-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", line)
            reqs[m.group(1).lower().replace("_", "-")] = m.group(2).replace(" ", "")
    return reqs


def test_desktop_requirements_track_the_backend_pins():
    backend_reqs = _requirements(REPO_ROOT / "backend" / "requirements.txt")
    desktop_reqs = _requirements(REPO_ROOT / "packaging" / "requirements-desktop.txt")
    server_only = {"asyncpg", "redis"}
    for name, spec in backend_reqs.items():
        if name in server_only:
            assert name not in desktop_reqs
            continue
        assert name in desktop_reqs, f"{name} is missing from packaging/requirements-desktop.txt"
        assert desktop_reqs[name] == spec, f"{name}: desktop pins {desktop_reqs[name]!r}, backend {spec!r}"
    assert desktop_reqs["pyinstaller"].startswith("==")


async def test_sidecar_server_streams_websockets(db_tables, desktop_mode, tmp_path, monkeypatch):
    """The real uvicorn configuration of the sidecar (h11 + websockets) carries the market stream."""
    import websockets

    from backend.main import create_app

    monkeypatch.setattr(ws_mod, "market_data_service", FakeMarket({"AAPL": make_trending(2)}))

    async def idle_bot(stop):
        await stop.wait()

    app = create_app()
    task, ctx = await _serve_in_process(app, tmp_path, idle_bot)
    try:
        async with httpx.AsyncClient(base_url=ctx.url) as http:
            user = {"email": "owner@example.com", "username": "owner", "password": "correct-horse-battery"}
            assert (await http.post("/auth/register", json=user)).status_code == 403   # not from the app window
            assert (await http.post("/auth/register", json=user, headers=APP_WINDOW)).status_code == 201
            tok = (await http.post("/auth/login", json={"email": user["email"],
                                                        "password": user["password"]})).json()
            assert (await http.get("/", headers={"Host": "attacker.test"})).status_code == 400
        uri = ctx.url.replace("http://", "ws://") + f"/ws/market/AAPL?token={tok['access_token']}"
        async with websockets.connect(uri, open_timeout=10) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert msg["type"] == "quote" and msg["data"]["symbol"] == "AAPL"
            await ws.send(json.dumps({"type": "ping"}))
            assert json.loads(await asyncio.wait_for(ws.recv(), 10)) == {"type": "pong"}

        # DNS rebinding over a WebSocket upgrade: a foreign Host is refused before the app runs.
        def foreign_upgrade() -> bytes:
            with socket.create_connection(("127.0.0.1", ctx.port), timeout=10) as raw:
                raw.sendall((f"GET /ws/market/AAPL?token={tok['access_token']} HTTP/1.1\r\n"
                             f"Host: rebind.attacker.test:{ctx.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
                return raw.recv(1024)

        status_line = (await asyncio.to_thread(foreign_upgrade)).split(b"\r\n", 1)[0]
        assert status_line.startswith(b"HTTP/1.1 4") and b" 101 " not in status_line, status_line
    finally:
        app.state.request_shutdown("test")
        assert await asyncio.wait_for(task, 30) == 0
