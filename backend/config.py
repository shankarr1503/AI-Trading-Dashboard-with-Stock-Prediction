"""
Application configuration using Pydantic Settings.
Loads values from environment variables / .env file.
"""
from typing import List
from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    # ─── App ─────────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    APP_NAME: str = "AI Trading Platform"
    APP_VERSION: str = "1.0.0"

    # ─── Backend ─────────────────────────────────────────────────────────────
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000

    # ─── Database ────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://trading_user:trading_password@localhost:5432/trading_platform"

    # ─── Redis ───────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ─── JWT ─────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "change-this-secret-key-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ─── Market Data APIs ────────────────────────────────────────────────────
    YFINANCE_ENABLED: bool = True
    ALPHA_VANTAGE_API_KEY: str = ""
    ALPHA_VANTAGE_BASE_URL: str = "https://www.alphavantage.co/query"
    POLYGON_API_KEY: str = ""
    POLYGON_BASE_URL: str = "https://api.polygon.io"
    FINNHUB_API_KEY: str = ""
    FINNHUB_BASE_URL: str = "https://finnhub.io/api/v1"
    NSE_BASE_URL: str = "https://www.nseindia.com/api"

    # ─── ML Service ──────────────────────────────────────────────────────────
    ML_SERVICE_URL: str = "http://localhost:8001"
    MODEL_SAVE_DIR: str = "ml/saved_models"

    # ─── Sentiment ───────────────────────────────────────────────────────────
    NEWS_API_KEY: str = ""
    NEWS_API_BASE_URL: str = "https://newsapi.org/v2"

    # ─── Alerts ──────────────────────────────────────────────────────────────
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@aitradingplatform.com"

    # ─── CORS ────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:3001"]

    # ─── Rate Limiting ───────────────────────────────────────────────────────
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()
