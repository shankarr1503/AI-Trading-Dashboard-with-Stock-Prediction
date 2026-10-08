"""
Application configuration using Pydantic Settings.
Loads values from environment variables / .env file.
"""
import os
from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_DEFAULT = "change-this-secret-key-in-production"


def _env_file() -> Optional[str]:
    # The desktop sidecar (backend/desktop.py) loads <data dir>/settings.env into the
    # environment itself, so it must never pick up a stray .env from whatever
    # directory the app happened to be started in.
    if os.environ.get("DESKTOP_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    return ".env"


class Settings(BaseSettings):
    # `.env.example` documents keys consumed by other services (Postgres,
    # docker-compose, frontend), so unknown keys must be ignored, not rejected.
    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ─── App ─────────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    APP_NAME: str = "AI Trading Platform"
    APP_VERSION: str = "2.0.0"
    LOG_LEVEL: str = "INFO"

    # ─── Backend ─────────────────────────────────────────────────────────────
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000

    # ─── Database ────────────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite+aiosqlite:///./trading.db"
    # Create tables on startup. Convenient for local SQLite; production uses
    # `alembic upgrade head` and sets this to false.
    DB_AUTO_CREATE: bool = True

    # ─── Redis ───────────────────────────────────────────────────────────────
    REDIS_URL: str = ""

    # ─── JWT ─────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = INSECURE_JWT_DEFAULT
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ─── Market Data APIs ────────────────────────────────────────────────────
    YFINANCE_ENABLED: bool = True
    FINNHUB_API_KEY: str = ""
    FINNHUB_BASE_URL: str = "https://finnhub.io/api/v1"

    # ─── ML Service ──────────────────────────────────────────────────────────
    ML_SERVICE_URL: str = "http://localhost:8001"

    # ─── Sentiment ───────────────────────────────────────────────────────────
    NEWS_API_KEY: str = ""

    # ─── Accounts ────────────────────────────────────────────────────────────
    # Registration never grants admin outside development (there is no email
    # verification, so an email address proves nothing). Create the
    # administrator with `python -m backend.manage create-admin`.
    # In DESKTOP_MODE this flag is ignored: the first account becomes the
    # administrator and registration closes once any account exists.
    REGISTRATION_OPEN: bool = True

    # ─── Desktop app ─────────────────────────────────────────────────────────
    # Set by the desktop sidecar launcher (`python -m backend.desktop`), never by
    # hand: single-user mode on 127.0.0.1 serving the static frontend export.
    DESKTOP_MODE: bool = False
    # Where the sidecar keeps trading.db, secret.key, settings.env and logs/.
    TRADEBOT_DATA_DIR: str = ""
    # Static frontend export (frontend/out) served at "/" in desktop mode.
    TRADEBOT_STATIC_DIR: str = ""
    # Random per launch (set by the Electron shell). Enables /api/desktop/* when
    # set; requests must carry it in the X-Desktop-Token header.
    TRADEBOT_CONTROL_TOKEN: str = ""

    # ─── CORS ────────────────────────────────────────────────────────────────
    # Comma-separated list. Kept as a plain string because pydantic-settings
    # JSON-decodes list fields before validators run.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    # ─── Rate Limiting ───────────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: str = "120/minute"
    # Peers allowed to set X-Real-IP (the reverse proxy). Defaults cover Docker networks and loopback.
    TRUSTED_PROXY_CIDRS: str = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

    # ─── Trading bot ─────────────────────────────────────────────────────────
    # paper: simulated broker (default). alpaca_paper: Alpaca paper account.
    # alpaca_live: real money — additionally requires ALLOW_LIVE_TRADING=true.
    TRADING_MODE: str = "paper"
    ALLOW_LIVE_TRADING: bool = False
    ALPACA_API_KEY: str = ""
    ALPACA_API_SECRET: str = ""
    BOT_UNIVERSE: str = "AAPL,MSFT,GOOGL,AMZN,NVDA,META,JPM,V,WMT,JNJ"
    BOT_INITIAL_CAPITAL: float = 100_000.0
    BOT_CYCLE_MINUTES: int = 15
    # Manage positions the bot did not open (e.g. manual trades in the same
    # broker account). Off by default: use a dedicated account for the bot.
    BOT_ADOPT_EXTERNAL_POSITIONS: bool = False
    # With a real broker, refuse to open positions until a walk-forward
    # calibration with positive out-of-sample expectancy has been stored.
    BOT_REQUIRE_CALIBRATION_FOR_BROKER: bool = True
    # Operator alerts (kill switch, data faults, failed cycles) as JSON webhook.
    ALERT_WEBHOOK_URL: str = ""
    # Optional LLM risk reviewer (Claude). Can only veto or shrink trades.
    LLM_REVIEW_ENABLED: bool = False
    # Empty → the SDK's default credential chain (env var, `ant auth login` profile, ...).
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    # What to do if the reviewer errors out: "veto" (safe) or "approve".
    LLM_REVIEW_FAIL_MODE: str = "veto"

    # ─── Equity research ─────────────────────────────────────────────────────
    # Claude-written analyst reports (costs API credits; admins trigger new ones).
    # Without it, reports come from the deterministic quant model.
    RESEARCH_LLM_ENABLED: bool = False
    # Let the analyst use Anthropic's server-side web search for current news/filings.
    RESEARCH_WEB_SEARCH: bool = False
    RESEARCH_REPORT_MAX_AGE_HOURS: int = 24
    # Bot: no new entries this many calendar days before a scheduled earnings report.
    EARNINGS_BLACKOUT_DAYS: int = 3

    @field_validator("DATABASE_URL")
    @classmethod
    def use_async_driver(cls, v: str) -> str:
        """The app uses SQLAlchemy's async engine, so force async drivers."""
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://") or v.startswith("postgresql+psycopg2://"):
            v = "postgresql+asyncpg://" + v.split("://", 1)[1]
        if v.startswith("sqlite:///"):
            v = "sqlite+aiosqlite:///" + v[len("sqlite:///"):]
        return v

    @model_validator(mode="after")
    def check_production_safety(self) -> "Settings":
        # Fail closed: anything that is not explicitly a dev/test environment, or
        # that can touch a real broker, needs a strong secret.
        if not self.is_development or self.TRADING_MODE != "paper":
            if self.JWT_SECRET_KEY == INSECURE_JWT_DEFAULT or len(self.JWT_SECRET_KEY) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be set to a random value of at least 32 characters "
                    "outside development (e.g. `openssl rand -hex 32`)."
                )
        if self.TRADING_MODE not in ("paper", "alpaca_paper", "alpaca_live"):
            raise ValueError("TRADING_MODE must be one of: paper, alpaca_paper, alpaca_live")
        if self.TRADING_MODE == "alpaca_live" and not self.ALLOW_LIVE_TRADING:
            raise ValueError("TRADING_MODE=alpaca_live requires ALLOW_LIVE_TRADING=true")
        return self

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def bot_universe(self) -> List[str]:
        seen: List[str] = []
        for s in self.BOT_UNIVERSE.split(","):
            s = s.strip().upper()
            if s and s not in seen:
                seen.append(s)
        return seen

    @property
    def sync_database_url(self) -> str:
        """Synchronous URL for Alembic."""
        url = self.DATABASE_URL
        return url.replace("+asyncpg", "+psycopg2").replace("+aiosqlite", "")

    @property
    def is_development(self) -> bool:
        return self.APP_ENV.lower() in ("development", "dev", "test")


settings = Settings()
