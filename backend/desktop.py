"""
Desktop sidecar: the local backend of the "AI Trading Bot" desktop app.

    python -m backend.desktop [--data-dir DIR] [--port N] [--no-bot]       from source
    tradebot-backend          [--data-dir DIR] [--port N] [--no-bot]       PyInstaller build
    tradebot-backend create-admin --email E --username U                  recovery: (re)set an admin
    tradebot-backend revoke-tokens --email E                              recovery: sign out everywhere

One process serves the REST/WebSocket API *and* the static frontend export on
http://127.0.0.1:<port> (one origin), and runs the trading bot loop as a
background task. The Electron shell (desktop/) starts it and talks to it:

stdout carries exactly these lines (each flushed; logs go to stderr and
<data dir>/logs/backend.log, stray prints are redirected to stderr):

    TRADEBOT_READY {"url": "http://127.0.0.1:47821", "port": 47821, "data_dir": "...", "version": "..."}
    TRADEBOT_ERROR <message>          fatal startup error, followed by a non-zero exit

Environment (real environment variables always win over settings.env):

    TRADEBOT_PORT           first port to try (default 47821, then the next 20; 0 = any free port)
    TRADEBOT_DATA_DIR       data directory (default: per-user, see default_data_dir())
    TRADEBOT_CONTROL_TOKEN  enables POST /api/desktop/shutdown and GET /api/desktop/info
                            (header X-Desktop-Token); the shell generates one per launch
    TRADEBOT_STATIC_DIR     static frontend export to serve (default: the bundled one)
    TRADEBOT_NO_BOT=1       same as --no-bot: serve the app without the bot loop
    TRADEBOT_WATCH_PARENT=0 do not exit when the launching process disappears
    TRADEBOT_LOG_STDERR=0   log only to the log file

DATABASE_URL, JWT_SECRET_KEY, APP_ENV, REDIS_URL and the other keys in
PROTECTED_KEYS are always set by the launcher, never taken from settings.env or
the environment.

The data directory holds trading.db (SQLite, migrated with Alembic on every
start), secret.key (generated JWT secret), settings.env (user-editable
KEY=VALUE settings, created from a commented template), server.json (url, port
and pid of the running instance) and logs/.

Shutdown (control API, SIGINT/SIGTERM, or the launching process exiting) stops
the bot loop *between* cycles: a cycle in progress finishes first (at most
BOT_STOP_TIMEOUT seconds), then the HTTP server stops and the process exits 0.

Apart from backend.logging_utils, nothing from backend.* is imported at module
level: the environment has to be prepared before backend.config reads it.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, MutableMapping, Optional, Tuple

# Safe before the environment is prepared: logging_utils does not import backend.config.
from backend.logging_utils import RedactTokens, install_token_redaction

APP_NAME = "AI Trading Bot"
HOST = "127.0.0.1"
DEFAULT_PORT = 47821
PORT_FALLBACKS = 20                 # 47821 busy → try 47822..47841
BOT_STOP_TIMEOUT = 150.0            # seconds an in-flight cycle may take to finish on shutdown
HTTP_GRACEFUL_TIMEOUT = 5           # seconds open connections (WebSockets) get to close
PARENT_POLL_SECONDS = 2.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
DB_BACKUPS = 5                      # pre-migration copies of trading.db kept in <data dir>/backups
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
VERSION_FILE = "tradebot_version.txt"   # written into the PyInstaller bundle by packaging/

# Decided by the launcher (forced in configure_environment, or set by the Electron
# shell): settings.env cannot change them, and for most of them neither can the
# environment. The database and the JWT secret always live in the data directory.
PROTECTED_KEYS = frozenset({"DESKTOP_MODE", "APP_ENV", "DB_AUTO_CREATE", "TRADEBOT_DATA_DIR",
                            "TRADEBOT_CONTROL_TOKEN", "REDIS_URL", "DATABASE_URL", "JWT_SECRET_KEY",
                            "TRUSTED_PROXY_CIDRS", "BACKEND_HOST", "BACKEND_PORT"})

SETTINGS_TEMPLATE = """\
# AI Trading Bot settings
#
# One KEY=VALUE per line; lines starting with # are comments. To change a
# setting, remove the # in front of it, edit the value and restart the app.
# Environment variables set outside the app take precedence over this file.
# Keep this file private: it holds your API keys.

# ----- Trading ---------------------------------------------------------------
# paper         built-in simulator with virtual money (default)
# alpaca_paper  your Alpaca paper-trading account (needs the Alpaca keys below)
# alpaca_live   REAL MONEY - also requires ALLOW_LIVE_TRADING=true
#TRADING_MODE=paper
#ALLOW_LIVE_TRADING=false
#ALPACA_API_KEY=
#ALPACA_API_SECRET=

# Symbols the bot trades (comma-separated), minutes between cycles, and the
# starting balance of the built-in paper account.
#BOT_UNIVERSE=AAPL,MSFT,GOOGL,AMZN,NVDA,META,JPM,V,WMT,JNJ
#BOT_CYCLE_MINUTES=15
#BOT_INITIAL_CAPITAL=100000

# ----- Claude (optional) -----------------------------------------------------
# The risk reviewer can only veto or shrink trades the risk manager approved.
#ANTHROPIC_API_KEY=
#LLM_REVIEW_ENABLED=false
# What a failed review means: veto (safe) or approve.
#LLM_REVIEW_FAIL_MODE=veto
# Claude-written equity research reports (uses ANTHROPIC_API_KEY).
#RESEARCH_LLM_ENABLED=false
#RESEARCH_WEB_SEARCH=false

# ----- Alerts ----------------------------------------------------------------
# Slack/Discord/Teams-compatible JSON webhook for kill-switch and failure alerts.
#ALERT_WEBHOOK_URL=

# ----- Market data (optional) ------------------------------------------------
#FINNHUB_API_KEY=
#NEWS_API_KEY=

# ----- Advanced --------------------------------------------------------------
#LOG_LEVEL=INFO
#BOT_REQUIRE_CALIBRATION_FOR_BROKER=true
#BOT_ADOPT_EXTERNAL_POSITIONS=false
#EARNINGS_BLACKOUT_DAYS=3
#JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
#RATE_LIMIT_DEFAULT=600/minute
"""

logger = logging.getLogger("desktop")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StartupError(Exception):
    """A fatal startup problem, reported to the shell as TRADEBOT_ERROR."""


def _env_flag(name: str, default: bool, environ: Optional[MutableMapping[str, str]] = None) -> bool:
    value = (os.environ if environ is None else environ).get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


# ─── stdout protocol ──────────────────────────────────────────────────────────

_protocol_stream = None


def _claim_stdout() -> None:
    """Keep the real stdout for protocol lines; anything else printed goes to stderr."""
    global _protocol_stream
    if _protocol_stream is not None:
        return
    _protocol_stream = sys.stdout
    for stream, errors in ((sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors=errors)
    if sys.stderr is not None:
        sys.stdout = sys.stderr


def emit(kind: str, payload: str) -> None:
    """Write one protocol line (TRADEBOT_READY / TRADEBOT_ERROR) and flush it."""
    stream = _protocol_stream or sys.stdout
    if stream is None:      # pragma: no cover - windowed builds have no stdout
        return
    line = f"{kind} {payload}".replace("\r", " ").replace("\n", " ")
    with contextlib.suppress(Exception):
        stream.write(line + "\n")
        stream.flush()


# ─── Paths ────────────────────────────────────────────────────────────────────

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where bundled files live: the PyInstaller bundle, or the repository root from source."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent.parent


def migrations_dir() -> Path:
    return bundle_dir() / "backend" / "database" / "migrations"


def default_static_dir() -> Optional[Path]:
    candidate = bundle_dir() / ("frontend_out" if is_frozen() else os.path.join("frontend", "out"))
    return candidate if (candidate / "index.html").is_file() else None


def default_data_dir(platform: Optional[str] = None, environ: Optional[MutableMapping[str, str]] = None,
                     home: Optional[Path] = None) -> Path:
    """Per-user data directory (the same one the Electron shell uses)."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = home or Path.home()
    if platform.startswith("win"):
        appdata = environ.get("APPDATA")
        return (Path(appdata) if appdata else home / "AppData" / "Roaming") / APP_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    return home / ".local" / "share" / "ai-trading-bot"


def resolve_data_dir(cli_value: Optional[str] = None, environ: Optional[MutableMapping[str, str]] = None) -> Path:
    environ = os.environ if environ is None else environ
    raw = cli_value or environ.get("TRADEBOT_DATA_DIR") or ""
    path = Path(raw).expanduser() if raw.strip() else default_data_dir(environ=environ)
    return path.resolve()


def _make_private_dir(path: Path) -> None:
    existed = path.is_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StartupError(f"Cannot create {path}: {e}") from e
    if not existed and os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)


def prepare_data_dir(data_dir: Path) -> None:
    _make_private_dir(data_dir)
    _make_private_dir(data_dir / "logs")
    if not os.access(data_dir, os.W_OK):
        raise StartupError(f"The data directory {data_dir} is not writable")


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via a temp file in the same directory + rename: readers never see a partial file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


# ─── secret.key ───────────────────────────────────────────────────────────────

def ensure_secret(data_dir: Path) -> str:
    """The JWT signing secret: generated once (64 hex chars, mode 0600) and reused on every start."""
    path = data_dir / "secret.key"
    with contextlib.suppress(FileNotFoundError):
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
        logger.warning("secret.key is unusable (too short); generating a new one - existing sessions end")
    secret = secrets.token_hex(32)
    atomic_write(path, secret + "\n", mode=0o600)
    return secret


# ─── settings.env ─────────────────────────────────────────────────────────────

def parse_env_text(text: str) -> Tuple[Dict[str, str], List[str]]:
    """
    Parse KEY=VALUE lines. Supports comments (#), blank lines, an optional
    `export ` prefix, single/double-quoted values and ` # trailing comments`
    after unquoted values. Returns (values, problems).
    """
    values: Dict[str, str] = {}
    problems: List[str] = []
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            problems.append(f"settings.env line {number}: expected KEY=VALUE, ignored")
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end == -1:
                problems.append(f"settings.env line {number}: unterminated quote in {key}, ignored")
                continue
            value = value[1:end]
        else:
            comment = re.search(r"\s#", value)
            if comment:
                value = value[:comment.start()].rstrip()
        values[key] = value
    return values, problems


def ensure_settings_file(data_dir: Path) -> Tuple[Path, bool]:
    """Create settings.env from the commented template on first run. Returns (path, created)."""
    path = data_dir / "settings.env"
    if path.exists():
        return path, False
    atomic_write(path, SETTINGS_TEMPLATE, mode=0o600)
    return path, True


def load_settings_env(path: Path, environ: Optional[MutableMapping[str, str]] = None) -> Tuple[List[str], List[str]]:
    """Load settings.env into the environment without overriding real environment variables."""
    environ = os.environ if environ is None else environ
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError) as e:
        raise StartupError(f"Cannot read {path}: {e}") from e
    values, problems = parse_env_text(text)
    applied: List[str] = []
    for key, value in values.items():
        if key in PROTECTED_KEYS:
            problems.append(f"settings.env: {key} is managed by the app, ignored")
            continue
        if key in environ:
            continue
        environ[key] = value
        applied.append(key)
    return applied, problems


# ─── Environment ──────────────────────────────────────────────────────────────

def configure_environment(data_dir: Path, port: int, static_dir: Optional[Path] = None,
                          environ: Optional[MutableMapping[str, str]] = None) -> None:
    """
    Desktop invariants (forced, whatever the environment says) and defaults (only
    where nothing is set) for backend.config. A DATABASE_URL or JWT_SECRET_KEY
    exported for some other project must never redirect the desktop app: its
    database and signing secret always live in the data directory.
    """
    environ = os.environ if environ is None else environ
    environ["DESKTOP_MODE"] = "true"
    environ["APP_ENV"] = "desktop"            # not development: requires the strong generated secret
    environ["DB_AUTO_CREATE"] = "false"       # the schema comes from Alembic migrations
    environ["TRADEBOT_DATA_DIR"] = str(data_dir)
    environ["BACKEND_HOST"] = HOST
    environ["BACKEND_PORT"] = str(port)
    environ["JWT_SECRET_KEY"] = ensure_secret(data_dir)
    environ["DATABASE_URL"] = sqlite_url(data_dir / "trading.db")
    environ["REDIS_URL"] = ""                  # in-process cache and rate limits (no redis in the bundle)
    # No reverse proxy in front of the sidecar: never take the client address from
    # X-Real-IP (any local process could otherwise dodge the login rate limit).
    environ["TRUSTED_PROXY_CIDRS"] = ""
    environ.setdefault("CORS_ORIGINS", f"http://{HOST}:{port},http://localhost:{port}")
    # Every request (UI, extension) comes from 127.0.0.1, so the per-IP limit is shared.
    environ.setdefault("RATE_LIMIT_DEFAULT", "600/minute")
    environ.setdefault("LOG_LEVEL", "INFO")
    if static_dir is not None:
        environ.setdefault("TRADEBOT_STATIC_DIR", str(static_dir))


def setup_logging(log_dir: Path, level: str = "INFO", to_stderr: bool = True) -> None:
    """Root logging to <log dir>/backend.log (rotated, LOG_BACKUPS x LOG_MAX_BYTES) and stderr, tokens redacted."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    formatter = logging.Formatter(LOG_FORMAT)
    redact = RedactTokens()
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "backend.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact)
    root.addHandler(file_handler)
    if to_stderr and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(redact)
        root.addHandler(stream_handler)
    numeric = logging.getLevelName(str(level).strip().upper())
    root.setLevel(numeric if isinstance(numeric, int) else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # one INFO line per HTTP request otherwise
    install_token_redaction()
    logging.captureWarnings(True)


# ─── Port ─────────────────────────────────────────────────────────────────────

def parse_port(value: Optional[str]) -> int:
    if value is None or str(value).strip() == "":
        return DEFAULT_PORT
    try:
        port = int(str(value).strip())
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise StartupError(f"TRADEBOT_PORT must be a number between 0 and 65535, got {value!r}")
    return port


def _someone_listens(host: str, port: int) -> bool:
    """True if something already accepts connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        try:
            return probe.connect_ex((host, port)) == 0
        except OSError:
            return False


def bind_port(preferred: int = DEFAULT_PORT, host: str = HOST, fallbacks: int = PORT_FALLBACKS) -> socket.socket:
    """
    Bind and listen on the first free port of preferred..preferred+fallbacks (0 = any
    free port). Handing the bound socket to uvicorn leaves no window in which another
    process could take the port between choosing and serving it.
    """
    candidates = [0] if preferred == 0 else [p for p in range(preferred, preferred + fallbacks + 1) if p <= 65535]
    last_error: Optional[OSError] = None
    for port in candidates:
        if port and os.name != "nt" and _someone_listens(host, port):
            # SO_REUSEADDR (below) keeps restarts on the same port despite TIME_WAIT,
            # but on macOS/BSD it would also let us bind 127.0.0.1:port next to a
            # server listening on 0.0.0.0:port and shadow it. Skip ports in use.
            last_error = OSError(f"port {port} is in use")
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                # On Windows SO_REUSEADDR would let us steal a port that is in use.
                sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFB), 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)
            sock.set_inheritable(False)
            return sock
        except OSError as e:
            sock.close()
            last_error = e
    raise StartupError(f"No free port on {host} in {candidates[0]}-{candidates[-1]} ({last_error})")


# ─── Single instance per data directory ───────────────────────────────────────

class InstanceLock:
    """An OS file lock on <data dir>/tradebot.lock, released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


# ─── Backend bootstrap (after the environment is ready) ───────────────────────

def load_settings():
    """Import backend.config, turning validation errors into a short message without secret values."""
    from pydantic import ValidationError

    try:
        from backend.config import settings
    except ValidationError as e:
        details = "; ".join(
            (f"{'.'.join(str(p) for p in err.get('loc', ()))}: " if err.get("loc") else "") + str(err.get("msg", ""))
            for err in e.errors()
        )
        raise StartupError(f"Invalid settings (settings.env or environment): {details}") from None
    return settings


def _sqlite_file(database_url: str) -> Optional[Path]:
    """The database file of a SQLite URL (None for other databases or in-memory SQLite)."""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
    except Exception:
        return None
    if not url.get_backend_name() == "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def _stored_revision(db_file: Path) -> Optional[str]:
    import sqlite3

    try:
        uri = db_file.resolve().as_uri() + "?mode=ro"         # as_uri percent-encodes spaces, '#', '?'
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=30)) as db:
            row = db.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None         # a new database, or one Alembic never touched
    return row[0] if row else None


def backup_database(db_file: Path, backup_dir: Path, label: str, keep: int = DB_BACKUPS) -> Path:
    """Consistent copy (SQLite backup API: WAL-safe) into backup_dir; keeps the newest `keep`."""
    import sqlite3
    import time

    _make_private_dir(backup_dir)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:40]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"trading-{stamp}-{safe_label}.db"
    counter = 1
    while target.exists():
        counter += 1
        target = backup_dir / f"trading-{stamp}-{safe_label}-{counter}.db"
    with contextlib.closing(sqlite3.connect(str(db_file), timeout=30)) as source, \
            contextlib.closing(sqlite3.connect(str(target))) as dest:
        source.backup(dest)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
    backups = sorted(backup_dir.glob("trading-*.db"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in backups[:-keep] if keep > 0 else []:
        if old != target:
            with contextlib.suppress(OSError):
                old.unlink()
    return target


def run_migrations(backup_dir: Optional[Path] = None) -> None:
    """
    `alembic upgrade head` against DATABASE_URL, from source or inside the PyInstaller
    bundle. With backup_dir, an existing SQLite database is copied there first
    whenever the upgrade has work to do (an app update must never cost the user
    their trading records).
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script_location = migrations_dir()
    if not (script_location / "env.py").is_file():
        raise StartupError(f"Database migrations not found at {script_location}")
    # No ini file: env.py then leaves logging alone (fileConfig would reset our handlers).
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location).replace("%", "%%"))
    try:
        head = ScriptDirectory.from_config(cfg).get_current_head()
        from backend.config import settings     # the URL env.py migrates

        db_file = _sqlite_file(settings.DATABASE_URL)
        if backup_dir is not None and db_file is not None and db_file.is_file():
            current = _stored_revision(db_file)
            if current is not None and current != head:
                target = backup_database(db_file, backup_dir, f"{current}-to-{head}")
                logger.info("Database upgrade %s -> %s: backup saved to %s", current, head, target)
        command.upgrade(cfg, "head")
    except StartupError:
        raise
    except Exception as e:
        logger.exception("Database migration failed")
        raise StartupError(f"Database migration failed: {type(e).__name__}: {e}") from e


def app_version(settings) -> str:
    """
    The desktop app version: stamped into the bundle by packaging/build_backend.py
    (from desktop/package.json); from source, desktop/package.json itself; else the
    backend's APP_VERSION.
    """
    with contextlib.suppress(OSError):
        stamped = (bundle_dir() / VERSION_FILE).read_text(encoding="utf-8").strip()
        if stamped:
            return stamped
    if not is_frozen():
        with contextlib.suppress(OSError, ValueError, AttributeError):
            version = json.loads((bundle_dir() / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]
            if isinstance(version, str) and version.strip():
                return version.strip()
    return settings.APP_VERSION


def _point_yfinance_cache(data_dir: Path) -> None:
    # yfinance keeps small sqlite caches (timezones, cookies); keep them with our data.
    try:
        import yfinance

        cache = data_dir / "cache" / "yfinance"
        cache.mkdir(parents=True, exist_ok=True)
        yfinance.set_tz_cache_location(str(cache))
    except Exception as e:  # pragma: no cover - optional
        logger.info("Could not move the yfinance cache: %s", e)


# ─── Shutdown ─────────────────────────────────────────────────────────────────

class Shutdown:
    """
    The one stop switch, flipped by the control API, signals or the parent
    watchdog. Safe to call from signal handlers and other threads. A second
    stop *signal* forces the exit without waiting for the bot cycle.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.requested = asyncio.Event()
        self.forced = asyncio.Event()
        self.reason: Optional[str] = None

    def request(self, reason: str = "requested", force_on_repeat: bool = False) -> None:
        with contextlib.suppress(RuntimeError):      # loop already closed
            self._loop.call_soon_threadsafe(self._request, reason, force_on_repeat)

    def _request(self, reason: str, force_on_repeat: bool) -> None:
        if not self.requested.is_set():
            self.reason = reason
            logger.info("Shutdown requested (%s)", reason)
            self.requested.set()
        elif force_on_repeat and not self.forced.is_set():
            logger.warning("Second stop signal: exiting without waiting for the trading cycle")
            self.forced.set()


def install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: Shutdown) -> None:
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):      # Ctrl+Break on Windows
        sigs.append(signal.SIGBREAK)
    for sig in sigs:
        reason = f"signal {sig.name}"
        try:
            loop.add_signal_handler(sig, shutdown.request, reason, True)
        except (NotImplementedError, RuntimeError, ValueError):
            with contextlib.suppress(ValueError, OSError):      # Windows: plain handlers
                signal.signal(sig, lambda _s, _f, r=reason: shutdown.request(r, True))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFO
            if not handle:
                return ctypes.get_last_error() == 5      # access denied: it exists
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0x102   # WAIT_TIMEOUT: still running
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


async def watch_parent(shutdown: Shutdown, parent_pid: int, interval: float = PARENT_POLL_SECONDS) -> None:
    """Exit gracefully when the process that launched us (the Electron shell) is gone."""
    while not shutdown.requested.is_set():
        await asyncio.sleep(interval)
        gone = (os.getppid() != parent_pid) if os.name != "nt" else not _pid_alive(parent_pid)
        if gone:
            shutdown.request("the launching process exited")
            return


# ─── Server ───────────────────────────────────────────────────────────────────

BotRunner = Callable[[asyncio.Event], Awaitable[None]]


@dataclass
class LaunchContext:
    data_dir: Path
    port: int
    url: str
    version: str
    run_bot: bool = True
    install_signals: bool = True
    parent_pid: Optional[int] = None         # watch this process; None = don't
    bot_stop_timeout: float = BOT_STOP_TIMEOUT


def _make_server(app, port: int):
    import uvicorn

    class SidecarServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            # The launcher owns SIGINT/SIGTERM: they must stop the bot between cycles
            # first, and the process then exits 0 instead of re-raising the signal.
            yield

    config = uvicorn.Config(
        app, host=HOST, port=port, loop="asyncio", http="h11", ws="websockets", lifespan="on",
        log_config=None, access_log=False, proxy_headers=False, server_header=False,
        timeout_graceful_shutdown=HTTP_GRACEFUL_TIMEOUT,
    )
    return SidecarServer(config)


async def _default_bot(stop: asyncio.Event) -> None:
    from backend.trading.agent import TradingAgent
    from backend.trading.runner import run_forever

    await run_forever(stop, TradingAgent(owner="desktop"))


async def _stop_bot(task: asyncio.Task, shutdown: Shutdown, timeout: float) -> None:
    if not task.done():
        logger.info("Stopping the trading bot (a cycle in progress may take up to %ds to finish)", int(timeout))
        forced = asyncio.ensure_future(shutdown.forced.wait())
        await asyncio.wait({task, forced}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        forced.cancel()
        if not task.done():
            logger.warning("Trading cycle still running: cancelling it")
            task.cancel()
            await asyncio.wait({task}, timeout=15)
    if task.done() and not task.cancelled() and task.exception() is not None:
        logger.error("Trading bot loop failed", exc_info=task.exception())


def _remove_server_file(path: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()


async def serve_app(app, sock: socket.socket, ctx: LaunchContext, bot_runner: Optional[BotRunner] = None) -> int:
    """
    Serve `app` on the already-bound `sock` until a shutdown is requested; returns
    the exit code. TRADEBOT_READY is printed once the server accepts connections.
    """
    loop = asyncio.get_running_loop()
    shutdown = Shutdown(loop)
    app.state.request_shutdown = shutdown.request
    app.state.desktop_url = ctx.url
    app.state.desktop_version = ctx.version
    if ctx.install_signals:
        install_signal_handlers(loop, shutdown)

    server = _make_server(app, ctx.port)
    server_task = asyncio.create_task(server.serve(sockets=[sock]), name="http-server")
    while not server.started:
        if server_task.done():
            error = None if server_task.cancelled() else server_task.exception()
            raise StartupError("The HTTP server did not start" + (f": {error}" if error else " (see logs/backend.log)"))
        await asyncio.sleep(0.02)

    server_file = ctx.data_dir / "server.json"
    atomic_write(server_file, json.dumps({"url": ctx.url, "port": ctx.port, "pid": os.getpid()}) + "\n")
    logger.info("Serving %s (data: %s)", ctx.url, ctx.data_dir)
    emit("TRADEBOT_READY", json.dumps({"url": ctx.url, "port": ctx.port, "data_dir": str(ctx.data_dir),
                                       "version": ctx.version}))

    bot_stop = asyncio.Event()
    bot_task: Optional[asyncio.Task] = None
    if ctx.run_bot:
        bot_task = asyncio.create_task((bot_runner or _default_bot)(bot_stop), name="trading-bot")
        bot_task.add_done_callback(
            lambda t: t.cancelled() or t.exception() is None or bot_stop.is_set()
            or logger.error("Trading bot loop crashed; the app keeps serving without it", exc_info=t.exception()))
    else:
        logger.info("Trading bot loop disabled (--no-bot)")
    watchdog = (asyncio.create_task(watch_parent(shutdown, ctx.parent_pid), name="parent-watchdog")
                if ctx.parent_pid else None)

    stop_wait = asyncio.ensure_future(shutdown.requested.wait())
    await asyncio.wait({stop_wait, server_task}, return_when=asyncio.FIRST_COMPLETED)
    exit_code = 0
    if server_task.done() and not shutdown.requested.is_set():
        logger.error("The HTTP server stopped unexpectedly")
        exit_code = 1

    # 1) The bot: let a cycle in progress finish, so no order is cut off mid-flight.
    bot_stop.set()
    if bot_task is not None:
        await _stop_bot(bot_task, shutdown, ctx.bot_stop_timeout)
    # 2) The HTTP server: stop accepting, give open connections a moment, run lifespan shutdown.
    server.should_exit = True
    if shutdown.forced.is_set():
        server.force_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(server_task), timeout=HTTP_GRACEFUL_TIMEOUT + 25)
    except asyncio.TimeoutError:
        logger.error("The HTTP server did not stop in time")
        server_task.cancel()
        exit_code = 1
    except Exception:
        logger.exception("The HTTP server failed")
        exit_code = 1
    for task in (stop_wait, watchdog):
        if task is not None:
            task.cancel()
    _remove_server_file(server_file)
    logger.info("Stopped (%s)", shutdown.reason or "server exit")
    return exit_code


# ─── Commands ─────────────────────────────────────────────────────────────────

def _prepare(data_dir_arg: Optional[str], quiet: bool) -> Path:
    """Data directory, settings.env and logging: the steps that need nothing from backend.*."""
    data_dir = resolve_data_dir(data_dir_arg)
    prepare_data_dir(data_dir)
    settings_path, created = ensure_settings_file(data_dir)
    _, problems = load_settings_env(settings_path)
    level = "WARNING" if quiet else os.environ.get("LOG_LEVEL", "INFO")
    setup_logging(data_dir / "logs", level, to_stderr=_env_flag("TRADEBOT_LOG_STDERR", True))
    if created:
        logger.info("Created %s from the template", settings_path)
    for problem in problems:
        logger.warning(problem)
    if is_frozen():
        # Never write __pycache__ into the installed (possibly code-signed) app bundle.
        sys.dont_write_bytecode = True
    return data_dir


def _configure(data_dir: Path, port: int):
    """Prepare the environment, then import (and validate) backend.config."""
    configure_environment(data_dir, port, default_static_dir())
    return load_settings()


def run_until_done(main: Awaitable[int], executor_grace: float = 10.0) -> int:
    """
    Like asyncio.run, but a blocking call still running in a worker thread (a slow
    market-data request) cannot hold up the exit for long: Python 3.11 would wait
    for it with no timeout.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code, lingering = 1, False
    try:
        code = loop.run_until_complete(main)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.wait(pending, timeout=5))
            loop.run_until_complete(loop.shutdown_asyncgens())
            try:
                loop.run_until_complete(asyncio.wait_for(loop.shutdown_default_executor(), executor_grace))
            except asyncio.TimeoutError:
                lingering = True
        finally:
            asyncio.set_event_loop(None)
            loop.close()
    if lingering:
        logger.warning("Blocking calls were still running %ss after shutdown; exiting anyway", executor_grace)
        logging.shutdown()
        os._exit(code)
    return code


def cmd_serve(args) -> int:
    _claim_stdout()
    lock: Optional[InstanceLock] = None
    sock: Optional[socket.socket] = None
    try:
        data_dir = _prepare(args.data_dir, quiet=False)
        lock = InstanceLock(data_dir / "tradebot.lock")
        if not lock.acquire():
            lock = None
            raise StartupError(f"{APP_NAME} is already running with the data directory {data_dir}")
        port_arg = args.port if args.port is not None else os.environ.get("TRADEBOT_PORT")
        sock = bind_port(parse_port(None if port_arg is None else str(port_arg)))
        port = sock.getsockname()[1]
        url = f"http://{HOST}:{port}"
        settings = _configure(data_dir, port)
        logger.info("%s backend starting: data=%s, mode=%s, frontend=%s", APP_NAME, data_dir, settings.TRADING_MODE,
                    settings.TRADEBOT_STATIC_DIR or "(none: API only)")
        _point_yfinance_cache(data_dir)
        run_migrations(backup_dir=data_dir / "backups")

        from backend.main import app

        parent = os.getppid() if _env_flag("TRADEBOT_WATCH_PARENT", True) else 0
        ctx = LaunchContext(
            data_dir=data_dir, port=port, url=url, version=app_version(settings),
            run_bot=not (args.no_bot or _env_flag("TRADEBOT_NO_BOT", False)),
            parent_pid=parent if parent > 1 else None,
        )
        return run_until_done(serve_app(app, sock, ctx))
    except StartupError as e:
        logger.error("%s", e)
        emit("TRADEBOT_ERROR", str(e))
        return 1
    except KeyboardInterrupt:
        emit("TRADEBOT_ERROR", "Interrupted")
        return 130
    except Exception as e:
        logger.exception("Fatal error")
        emit("TRADEBOT_ERROR", f"{type(e).__name__}: {e}")
        return 1
    finally:
        if sock is not None:
            sock.close()
        if lock is not None:
            lock.release()


def _read_new_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD")
    if password:
        return password
    password = getpass.getpass("New password (12+ characters): ")
    if password != getpass.getpass("Repeat the password: "):
        raise StartupError("The passwords do not match")
    return password


def cmd_manage(args) -> int:
    """create-admin / revoke-tokens against the data directory's database (backend.manage)."""
    try:
        data_dir = _prepare(args.data_dir, quiet=True)
        _configure(data_dir, DEFAULT_PORT)
        # the app may never have started: make sure the tables exist
        run_migrations(backup_dir=data_dir / "backups")
        from backend import manage

        if args.command == "create-admin":
            password = _read_new_password()
            if len(password) < 12:
                print("Password must be at least 12 characters", file=sys.stderr)
                return 1
            action = asyncio.run(manage.create_admin(args.email, args.username, password))
            print(f"Admin {args.email} {action}")
            return 0
        ok = asyncio.run(manage.revoke(args.email))
        print("Tokens revoked" if ok else "No such user")
        return 0 if ok else 1
    except StartupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    prog = "tradebot-backend" if is_frozen() else "python -m backend.desktop"
    # Options are accepted before or after the subcommand. SUPPRESS keeps a subparser
    # from resetting a value the main parser already set; cli() fills the defaults.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="data directory (default: TRADEBOT_DATA_DIR, else the per-user app data directory)")
    serve_opts = argparse.ArgumentParser(add_help=False)
    serve_opts.add_argument("--port", type=int, default=argparse.SUPPRESS,
                            help=f"first port to try (default: TRADEBOT_PORT or {DEFAULT_PORT}; 0 = any free port)")
    serve_opts.add_argument("--no-bot", action="store_true", default=argparse.SUPPRESS,
                            help="do not run the trading bot loop (TRADEBOT_NO_BOT=1)")
    parser = argparse.ArgumentParser(prog=prog, parents=[common, serve_opts],
                                     description=f"{APP_NAME} backend: API + web app on 127.0.0.1 and the trading bot.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("serve", parents=[common, serve_opts], help="run the backend (the default)")
    admin = sub.add_parser("create-admin", parents=[common],
                           help="create an administrator, or reset an account's password and make it admin "
                                "(password from ADMIN_PASSWORD or a prompt)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--username", required=True)
    revoke = sub.add_parser("revoke-tokens", parents=[common], help="sign an account out everywhere")
    revoke.add_argument("--email", required=True)
    return parser


def cli(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in (("data_dir", None), ("port", None), ("no_bot", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if args.command in ("create-admin", "revoke-tokens"):
        return cmd_manage(args)
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(cli())
