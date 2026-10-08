"""
End-to-end tests of the Chrome extension (extension/) in Chromium.

The extension is packed with extension/scripts/pack.mjs and the *extracted ZIP*
is loaded unpacked, so the shipped package is what gets tested (Chrome
validates its manifest); without Node.js the source folder is loaded instead.

test_extension_end_to_end — against the real desktop sidecar started from source
  (`python -m backend.desktop --no-bot`, temporary data dir, first port free in
  47821..47841, first registered account = administrator): "Find the desktop
  app", options sign-in, popup PAUSED → Start (RUNNING, checked through the API)
  → Pause, recent decisions, the two-step panic flatten, the ticker panel with
  market data offline (errors must be shown gracefully), the badge the service
  worker sets in every state, sign-out and server shutdown.

test_popup_renders_live_bot_data — the real API (backend.main:app under uvicorn)
  with the test suite's FakeMarket instead of Yahoo and seeded bot rows, so the
  popup renders genuine responses: metrics, positions with unrealised P&L,
  decisions, the bot's analysis and the research fundamentals.

Needs Playwright for Python and a Chromium build; skipped otherwise (or with
SKIP_EXTENSION_E2E=1). Chromium comes from CHROMIUM_EXECUTABLE, then from
Playwright's browser folders. Headed when DISPLAY is set (run it under
`xvfb-run -a`), headless otherwise (Chromium's new headless mode loads
extensions). EXTENSION_E2E_SCREENSHOTS=<dir> saves screenshots of each state.
"""
import contextlib
import glob
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

if os.environ.get("SKIP_EXTENSION_E2E") == "1":
    pytest.skip("SKIP_EXTENSION_E2E=1", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright for Python is not installed")

REPO = Path(__file__).resolve().parents[1]
EXTENSION = REPO / "extension"
EMAIL = "owner@example.com"
USERNAME = "owner"
PASSWORD = "e2e-Password-123"
CONTROL_TOKEN = "e2e-control-token"
DESKTOP_PORTS = range(47821, 47842)
# conftest.py configures the in-process test app; the sidecar must start from a clean environment.
CONFTEST_KEYS = ("APP_ENV", "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "TRADING_MODE", "BOT_UNIVERSE")

# Direct connections only: an HTTP(S)_PROXY in the environment must not see loopback traffic.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _chromium():
    explicit = os.environ.get("CHROMIUM_EXECUTABLE")
    if explicit:
        return explicit if Path(explicit).exists() else None
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "/opt/pw-browsers", "~/.cache/ms-playwright"]
    for root in filter(None, roots):
        hits = sorted(glob.glob(os.path.join(os.path.expanduser(root), "chromium-*", "chrome-linux", "chrome")))
        if hits:
            return hits[-1]
    try:
        with sync_api.sync_playwright() as p:
            path = p.chromium.executable_path
        return path if path and Path(path).exists() else None
    except Exception:
        return None


CHROMIUM = _chromium()
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no Chromium build found (set CHROMIUM_EXECUTABLE)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _api(url: str, path: str, method: str = "GET", body=None, token=None, headers=None):
    req = urllib.request.Request(url + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=60) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CONFTEST_KEYS and not k.startswith("TRADEBOT_")}
    env.update({"PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")])),
                "PYTHONUNBUFFERED": "1", **extra})
    return env


def _shot(page, name: str) -> None:
    """Save a screenshot when EXTENSION_E2E_SCREENSHOTS names a folder (for reviewing the UI)."""
    folder = os.environ.get("EXTENSION_E2E_SCREENSHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


# ─── Fixtures ────────────────────────────────────────────────────────────────

class Sidecar:
    def __init__(self, proc: subprocess.Popen, url: str, log: Path):
        self.proc, self.url, self.log = proc, url, log

    def admin_token(self) -> str:
        status, body = _api(self.url, "/auth/login", "POST", {"email": EMAIL, "password": PASSWORD})
        assert status == 200, body
        return body["access_token"]

    def bot_status(self) -> dict:
        status, body = _api(self.url, "/api/bot/status", token=self.admin_token())
        assert status == 200, body
        return body


@pytest.fixture(scope="module")
def sidecar(tmp_path_factory):
    """The desktop sidecar from source, as the Electron shell starts it (minus the bot loop)."""
    data_dir = tmp_path_factory.mktemp("tradebot-data")
    log = data_dir / "sidecar-stderr.log"
    # The contract's port range (47821, else the next free one): "Find the desktop app" must see it.
    env = _clean_env(TRADEBOT_PORT="47821", TRADEBOT_DATA_DIR=str(data_dir), TRADEBOT_CONTROL_TOKEN=CONTROL_TOKEN)
    with open(log, "w") as err:
        proc = subprocess.Popen([sys.executable, "-m", "backend.desktop", "--no-bot"], cwd=REPO, env=env,
                                stdout=subprocess.PIPE, stderr=err, text=True)
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
    url = None
    deadline = time.monotonic() + 120
    while url is None and time.monotonic() < deadline:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line.startswith("TRADEBOT_READY "):
            url = json.loads(line.split(" ", 1)[1])["url"]
        elif line.startswith("TRADEBOT_ERROR"):
            break
    if url is None:
        proc.kill()
        pytest.fail(f"sidecar did not start:\n{log.read_text()[-4000:]}")

    status, body = _api(url, "/auth/register", "POST", {"email": EMAIL, "username": USERNAME, "password": PASSWORD})
    assert status == 201, body
    assert body["is_superuser"] is True, "desktop mode: the first account is the administrator"
    yield Sidecar(proc, url, log)
    if proc.poll() is None:
        _api(url, "/api/desktop/shutdown", "POST", headers={"X-Desktop-Token": CONTROL_TOKEN})
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


# The real API (backend.main:app, test settings) with FakeMarket instead of Yahoo
# and a seeded bot: running, two open positions, decisions, an equity snapshot.
_FAKE_MARKET_SERVER = r'''
import asyncio, sys
from datetime import timedelta

port, repo = int(sys.argv[1]), sys.argv[2]
sys.path.insert(0, repo)
from tests.conftest import FakeMarket, FakePredictor, make_ohlcv, make_trending   # offline test settings
from tests.research_fixtures import make_snapshot
import backend.market_data.router as market_router_mod
import backend.research.service as research_mod
import backend.signals.service as signals_mod
import backend.trading.router as bot_router_mod
from backend.auth.utils import get_password_hash
from backend.database.models import BotDecision, BotPosition, EquitySnapshot, User
from backend.database.session import AsyncSessionLocal, Base, engine, utcnow
from backend.main import app
from backend.trading.agent import get_state

fake = FakeMarket({"MSFT": make_trending(1), "AAPL": make_ohlcv(2, n=500)})
for mod in (market_router_mod, signals_mod, bot_router_mod):
    mod.market_data_service = fake
signals_mod.prediction_service = FakePredictor()
research_mod.research_service.market = fake
research_mod.research_service.snapshot_fetcher = lambda s: make_snapshot(s, price=300.0)
msft = float(fake.frames["MSFT"]["close"].iloc[-1])
aapl = float(fake.frames["AAPL"]["close"].iloc[-1])

async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        db.add(User(email="%(email)s", username="%(username)s", hashed_password=get_password_hash("%(password)s"),
                    is_superuser=True))
        state = await get_state(db)
        now = utcnow()
        state.enabled = True
        state.last_cycle_at = state.last_success_at = now - timedelta(minutes=2)
        state.high_water_mark = 105000.0
        state.last_cycle_summary = {"status": "ok", "equity": 102500.0, "daily_pnl_pct": -0.42, "entries": [], "exits": []}
        db.add(EquitySnapshot(timestamp=now, equity=102500.0, cash=60000.0, exposure=42500.0, drawdown_pct=2.381))
        db.add(BotPosition(symbol="MSFT", qty=50, entry_price=round(msft * 0.95, 2), stop_price=round(msft * 0.9, 2),
                           initial_stop=round(msft * 0.88, 2), target_price=round(msft * 1.2, 2), highest_price=msft,
                           entry_score=0.61, opened_at=now - timedelta(days=3)))
        db.add(BotPosition(symbol="AAPL", qty=12.5, entry_price=round(aapl * 1.04, 2), stop_price=round(aapl * 0.97, 2),
                           initial_stop=round(aapl * 0.96, 2), target_price=round(aapl * 1.15, 2), highest_price=aapl * 1.05,
                           entry_score=0.55, opened_at=now - timedelta(days=1)))
        db.add(BotDecision(cycle_id="c-1", symbol="MSFT", action="BUY", score=0.61, regime="bull_trend",
                           reasons=["Regime bull_trend; technical score +0.61"], created_at=now - timedelta(minutes=30)))
        db.add(BotDecision(cycle_id="c-2", symbol="NVDA", action="SKIP", score=0.12,
                           reasons=["Expected edge below round-trip costs"], created_at=now - timedelta(minutes=2)))
        await db.commit()
    await engine.dispose()   # uvicorn runs on another event loop

asyncio.run(seed())
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
''' % {"email": EMAIL, "username": USERNAME, "password": PASSWORD}


@pytest.fixture(scope="module")
def market_server(tmp_path_factory):
    port = _free_port()
    log = tmp_path_factory.mktemp("fake-market") / "server.log"
    with open(log, "w") as out:
        proc = subprocess.Popen([sys.executable, "-c", _FAKE_MARKET_SERVER, str(port), str(REPO)], cwd=REPO,
                                env=_clean_env(), stdout=out, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"fake-market server exited:\n{log.read_text()[-4000:]}")
        try:
            if _api(url, "/health")[0] == 200:
                break
        except OSError:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("fake-market server did not start")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def extension_dir(tmp_path_factory) -> Path:
    """The packaged extension, extracted (falls back to the source folder without Node.js)."""
    node = shutil.which("node")
    if not node:
        return EXTENSION
    out = tmp_path_factory.mktemp("ext-dist")
    done = subprocess.run([node, str(EXTENSION / "scripts" / "pack.mjs"), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    zips = list(out.glob("ai-trading-bot-extension-*.zip"))
    assert len(zips) == 1
    target = tmp_path_factory.mktemp("ext-unpacked")
    with zipfile.ZipFile(zips[0]) as z:
        names = z.namelist()
        assert "manifest.json" in names
        assert not [n for n in names if n.startswith(("tests/", "scripts/")) or n in ("package.json", "README.md")]
        z.extractall(target)
    return target


class Browser:
    """Chromium with the extension loaded: pages, the service worker and the toolbar badge."""

    def __init__(self, ctx):
        self.ctx = ctx
        worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=30_000)
        self.ext_id = worker.url.split("/")[2]
        assert worker.url == f"chrome-extension://{self.ext_id}/background.js"
        self.page_errors: list = []
        # A fresh install opens the settings page (onInstalled; Chrome reuses the blank start tab).
        # Wait for it and navigate it away, so it cannot take over a page the test opens later.
        # (Sleeping would not do: the sync API only learns about new pages inside its own calls.)
        options_url = f"chrome-extension://{self.ext_id}/options.html"
        deadline = time.monotonic() + 15
        auto = None
        while auto is None and time.monotonic() < deadline:
            auto = next((pg for pg in ctx.pages if pg.url == options_url), None)
            if auto is None:
                ctx.pages[0].wait_for_timeout(100) if ctx.pages else self.sw().evaluate("0")
        assert auto is not None, "installing the extension should open its settings page"
        auto.goto("about:blank")

    def sw(self):
        # Chrome may restart an idle worker: always talk to the current one.
        return self.ctx.service_workers[-1] if self.ctx.service_workers else self.ctx.wait_for_event("serviceworker")

    def open(self, name: str, width: int | None = None):
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.page_errors.append((name, str(e))))
        if width:
            page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"chrome-extension://{self.ext_id}/{name}")
        return page

    def badge(self) -> str:
        return self.sw().evaluate("chrome.action.getBadgeText({})")

    def title(self) -> str:
        return self.sw().evaluate("chrome.action.getTitle({})")

    def wait_badge(self, text: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        seen = None
        while time.monotonic() < deadline:
            seen = self.badge()
            if seen == text:
                return
            time.sleep(0.25)
        raise AssertionError(f"badge is {seen!r}, expected {text!r}; title: {self.title()!r}")

    def sign_in(self, server_url: str):
        options = self.open("options.html")
        options.fill("#server-url", server_url)
        options.click("#server-save")
        sync_api.expect(options.locator("#server-msg")).to_contain_text("Connected", timeout=20_000)
        options.fill("#email", EMAIL)
        options.fill("#password", PASSWORD)
        options.click("#sign-in")
        sync_api.expect(options.locator("#account-msg")).to_contain_text(f"Signed in as {USERNAME}", timeout=20_000)
        return options


@contextlib.contextmanager
def _browser(extension_dir: Path, profile: Path):
    with sync_api.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            executable_path=CHROMIUM,
            headless=not os.environ.get("DISPLAY"),
            args=[
                f"--disable-extensions-except={extension_dir}",
                f"--load-extension={extension_dir}",
                "--no-proxy-server",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try:
            b = Browser(ctx)
            yield b
        finally:
            ctx.close()
        assert b.page_errors == [], b.page_errors


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_extension_end_to_end(sidecar: Sidecar, extension_dir: Path, tmp_path):
    expect = sync_api.expect
    with _browser(extension_dir, tmp_path / "profile") as b:
        manifest = b.sw().evaluate("chrome.runtime.getManifest()")
        assert manifest["name"] == "AI Trading Bot — Companion"
        assert manifest["manifest_version"] == 3
        alarm = None
        for _ in range(40):                     # created by onInstalled / worker start-up
            alarm = b.sw().evaluate("chrome.alarms.get('tradebot-poll')")
            if alarm:
                break
            time.sleep(0.25)
        assert alarm and alarm["periodInMinutes"] == 1, alarm

        # ── Options: find the desktop app, server URL rules, sign-in ─────────
        options = b.open("options.html")
        options.click("#server-find")
        expect(options.locator("#server-msg")).to_contain_text(sidecar.url, timeout=20_000)
        assert int(sidecar.url.rsplit(":", 1)[1]) in DESKTOP_PORTS
        options.fill("#server-url", "http://192.168.1.20:47821")
        options.click("#server-save")
        expect(options.locator("#server-msg")).to_contain_text("https://")      # plain http refused off-loopback
        options.fill("#server-url", sidecar.url + "/")
        options.click("#server-save")
        expect(options.locator("#server-msg")).to_contain_text("Connected", timeout=20_000)
        expect(options.locator("#server-url")).to_have_value(sidecar.url)
        b.wait_badge("?")                                                        # reachable, not signed in

        options.fill("#email", EMAIL)
        options.fill("#password", "wrong-password")
        options.click("#sign-in")
        expect(options.locator("#account-msg")).to_have_text("Wrong email or password.", timeout=20_000)
        options.fill("#password", PASSWORD)
        options.click("#sign-in")
        expect(options.locator("#account-msg")).to_contain_text(f"Signed in as {USERNAME}", timeout=20_000)
        expect(options.locator("#who-role")).to_have_text("ADMINISTRATOR")
        expect(options.locator("#password")).to_have_value("")
        _shot(options, "options-signed-in")

        # Tokens: access token only in session storage, refresh token in local storage, no password anywhere.
        session = b.sw().evaluate("chrome.storage.session.get(null)")
        local = b.sw().evaluate("chrome.storage.local.get(null)")
        assert session["access"]["server"] == sidecar.url and session["access"]["token"]
        assert local["auth"]["refreshToken"] and local["auth"]["user"]["is_superuser"] is True
        assert PASSWORD not in json.dumps(local) + json.dumps(session)
        assert session["access"]["token"] not in json.dumps(local)

        # The worker polled after sign-in: paused bot, never cycled, nothing open → not stale.
        b.wait_badge("OFF")
        assert "PAUSED (PAPER)" in b.title()
        expect(options.locator("#status-line")).to_contain_text("PAUSED", timeout=10_000)

        # ── Popup: state, positions, controls ────────────────────────────────
        popup = b.open("popup.html", width=360)
        expect(popup.locator("#bot-state")).to_have_text("PAUSED", timeout=20_000)
        expect(popup.locator("#bot-mode")).to_have_text("PAPER")
        expect(popup.locator("#conn-text")).to_contain_text(f"{USERNAME} (admin)")
        expect(popup.locator("#bot-updated")).to_have_text("updated just now")
        expect(popup.locator("#pos-msg")).to_have_text("No open positions.", timeout=20_000)
        expect(popup.locator("#btn-start")).to_be_enabled()
        expect(popup.locator("#btn-pause")).to_be_disabled()
        expect(popup.locator("#notice")).to_be_hidden()
        expect(popup.locator("#bot-warnings li")).to_have_count(0)
        assert popup.evaluate("document.documentElement.scrollWidth") <= 360, "no horizontal scroll at 360 px"
        popup.click("#decisions summary")
        expect(popup.locator("#dec-msg")).to_have_text("No decisions recorded yet.", timeout=20_000)
        _shot(popup, "popup-paused")

        popup.click("#btn-start")
        expect(popup.locator("#bot-msg")).to_contain_text("Bot started", timeout=20_000)
        expect(popup.locator("#bot-state")).to_have_text("RUNNING")
        assert sidecar.bot_status()["enabled"] is True
        # --no-bot: no cycle will ever run, and an enabled bot needs its loop → STALE warning,
        # LATE badge and exactly one notification.
        expect(popup.locator("#bot-warnings li[data-key='stale']")).to_contain_text("No trading cycle has run yet")
        b.wait_badge("LATE")
        _shot(popup, "popup-running-stale")
        monitor = b.sw().evaluate("chrome.storage.local.get('monitor')")["monitor"]
        assert monitor["conditions"]["stale"] is True
        assert list(b.sw().evaluate("chrome.notifications.getAll()")) == ["tradebot-stale"]
        b.sw().evaluate("tradebot.poll()")                     # an alarm firing again: no repeat
        assert list(b.sw().evaluate("chrome.notifications.getAll()")) == ["tradebot-stale"]

        popup.click("#btn-pause")
        expect(popup.locator("#bot-msg")).to_contain_text("Bot paused", timeout=20_000)
        expect(popup.locator("#bot-state")).to_have_text("PAUSED")
        assert sidecar.bot_status()["enabled"] is False
        b.wait_badge("OFF")

        with b.ctx.expect_page() as opened:
            popup.click("#btn-dashboard")
        assert opened.value.url.startswith(f"{sidecar.url}/bot/"), opened.value.url
        opened.value.close()

        # ── Panic flatten needs an explicit second step ──────────────────────
        popup.click("#btn-panic")
        expect(popup.locator("#panic-confirm")).to_be_visible()
        expect(popup.locator("#btn-panic")).to_be_hidden()
        _shot(popup, "popup-panic-confirm")
        popup.click("#btn-panic-cancel")
        expect(popup.locator("#panic-confirm")).to_be_hidden()
        assert sidecar.bot_status()["halted"] is False, "cancel must not flatten"

        popup.click("#btn-panic")
        popup.click("#btn-panic-confirm")
        expect(popup.locator("#bot-msg")).to_contain_text("Flatten complete: 0 position(s) closed", timeout=90_000)
        expect(popup.locator("#bot-state")).to_have_text("HALTED")
        expect(popup.locator("#bot-reason")).to_contain_text(f"FLATTEN: manual by {USERNAME}")
        expect(popup.locator("#btn-start")).to_be_disabled()
        status = sidecar.bot_status()
        assert status["halted"] is True and status["flatten_requested"] is False
        b.wait_badge("HALT")

        # ── Ticker panel (market data is offline here) ───────────────────────
        expect(popup.locator("#ticker")).to_be_visible()
        popup.fill("#ticker-input", "AA PL!")
        popup.click("#ticker-go")
        expect(popup.locator("#ticker-msg")).to_contain_text("valid symbol")
        popup.fill("#ticker-input", "aapl")
        popup.click("#ticker-go")
        expect(popup.locator("#ticker-input")).to_have_value("AAPL")
        expect(popup.locator("#analysis .msg.error")).to_be_visible(timeout=120_000)
        expect(popup.locator("#fundamentals .msg.error")).to_be_visible(timeout=120_000)
        assert "AAPL" in popup.locator("#analysis .msg.error").inner_text() + \
            popup.locator("#fundamentals .msg.error").inner_text()
        expect(popup.locator("#bot-state")).to_have_text("HALTED")   # the rest of the popup kept working
        _shot(popup, "popup-halted-ticker")

        # ── Sign out, then the server goes away ──────────────────────────────
        options.bring_to_front()
        options.click("#sign-out")
        expect(options.locator("#login-form")).to_be_visible()
        b.wait_badge("?")
        assert "auth" not in b.sw().evaluate("chrome.storage.local.get(null)")
        assert "access" not in b.sw().evaluate("chrome.storage.session.get(null)")

        status, body = _api(sidecar.url, "/api/desktop/shutdown", "POST", headers={"X-Desktop-Token": CONTROL_TOKEN})
        assert status == 202, body
        assert sidecar.proc.wait(timeout=60) == 0
        assert b.sw().evaluate("tradebot.poll().then(r => r.display.badge.text)") == "DOWN"   # the next alarm
        b.wait_badge("DOWN")
        popup.reload()
        expect(popup.locator("#notice-title")).to_have_text("Cannot reach the server", timeout=30_000)
        expect(popup.locator("#bot")).to_be_hidden()
        _shot(popup, "popup-server-down")


def test_popup_renders_live_bot_data(market_server: str, extension_dir: Path, tmp_path):
    expect = sync_api.expect
    with _browser(extension_dir, tmp_path / "profile") as b:
        b.sign_in(market_server)
        b.wait_badge("RUN")
        assert "RUNNING (PAPER)" in b.title() and "Open positions 2" in b.title()

        popup = b.open("popup.html", width=360)
        expect(popup.locator("#bot-state")).to_have_text("RUNNING", timeout=20_000)
        expect(popup.locator("#bot-warnings li")).to_have_count(0)
        expect(popup.locator("#m-equity")).to_have_text("$102,500.00")
        expect(popup.locator("#m-drawdown")).to_have_text("2.38%")
        expect(popup.locator("#m-daily")).to_have_text("−0.42% −$432")
        expect(popup.locator("#m-daily")).to_have_class("num down")
        expect(popup.locator("#m-cycle")).to_have_text("2 min ago · ok")
        expect(popup.locator("#btn-start")).to_be_disabled()
        expect(popup.locator("#btn-pause")).to_be_enabled()

        rows = popup.locator("#pos-body tr")
        expect(rows).to_have_count(2, timeout=20_000)
        expect(popup.locator("#pos-count")).to_have_text("(2)")
        by_symbol = {rows.nth(i).locator("td").first.inner_text(): rows.nth(i) for i in range(2)}
        assert set(by_symbol) == {"MSFT", "AAPL"}
        msft = by_symbol["MSFT"].locator("td")
        assert msft.nth(1).inner_text() == "50"
        assert msft.nth(4).get_attribute("class") == "r up" and msft.nth(4).inner_text().startswith("+$")
        aapl = by_symbol["AAPL"].locator("td")
        assert aapl.nth(1).inner_text() == "12.5"
        assert aapl.nth(4).get_attribute("class") == "r down" and aapl.nth(4).inner_text().startswith("−$")

        popup.click("#decisions summary")
        expect(popup.locator("#dec-list li")).to_have_count(2, timeout=20_000)
        expect(popup.locator("#dec-list li").first).to_contain_text("SKIP NVDA")
        expect(popup.locator("#dec-list li").nth(1)).to_contain_text("BUY MSFT")

        popup.fill("#ticker-input", "msft")
        popup.press("#ticker-input", "Enter")
        expect(popup.locator("#quote .qsym")).to_have_text("MSFT", timeout=30_000)
        expect(popup.locator("#analysis .signal")).to_have_text(re_signal(), timeout=60_000)
        analysis = popup.locator("#analysis").inner_text()
        for label in ("Technical score", "Blended score", "Regime", "Entry", "Edge vs costs"):
            assert label in analysis, analysis
        expect(popup.locator("#analysis .reasons li").first).to_contain_text("Regime")
        expect(popup.locator("#fundamentals h3")).to_have_text("Research", timeout=60_000)
        expect(popup.locator("#fundamentals")).to_contain_text("Composite score", timeout=60_000)
        fundamentals = popup.locator("#fundamentals").inner_text()
        assert "/100 (" in fundamentals and "/9" in fundamentals, fundamentals
        assert "Jan 30, 2030" in fundamentals, fundamentals          # make_snapshot's next earnings date
        for label in ("Fair value", "Piotroski F-score", "Altman zone", "Next earnings"):
            assert label in fundamentals, fundamentals
        assert popup.evaluate("document.documentElement.scrollWidth") <= 360, "no horizontal scroll at 360 px"
        _shot(popup, "popup-live-data")


def re_signal():
    import re

    return re.compile(r"^(BUY|SELL|HOLD)$")
