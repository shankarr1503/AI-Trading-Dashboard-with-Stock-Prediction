"""
Application configuration using Pydantic Settings.
Loads values from environment variables / .env file.
"""
from typing import List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_DEFAULT = "change-this-secret-key-in-production"


class Settings(BaseSettings):
    # `.env.example` documents keys consumed by other services (Postgres,
    # docker-compose, frontend), so unknown keys must be ignored, not rejected.
    model_config = SettingsConfigDict(
        env_file=".env",
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

    # ─── CORS ────────────────────────────────────────────────────────────────
    # Comma-separated list. Kept as a plain string because pydantic-settings
    # JSON-decodes list fields before validators run.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    # ─── Rate Limiting ───────────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: str = "120/minute"

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
    # Optional LLM risk reviewer (Claude). Can only veto or shrink trades.
    LLM_REVIEW_ENABLED: bool = False
    # Empty → the SDK's default credential chain (env var, `ant auth login` profile, ...).
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5-5"
    # What to do if the reviewer errors out: "veto" (safe) or "approve".
    LLM_REVIEW_FAIL_MODE: str = "veto"

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
        if self.APP_ENV.lower() in ("production", "prod", "staging"):
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
        return [s.strip().upper() for s in self.BOT_UNIVERSE.split(",") if s.strip()]

    @property
    def sync_database_url(self) -> str:
        """Synchronous URL for Alembic."""
        url = self.DATABASE_URL
        return url.replace("+asyncpg", "+psycopg2").replace("+aiosqlite", "")

    @property
    def is_development(self) -> bool:
        return self.APP_ENV.lower() in ("development", "dev", "test")


settings = Settings()
