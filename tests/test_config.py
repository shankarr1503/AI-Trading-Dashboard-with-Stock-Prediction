import pytest
from pydantic import ValidationError

from backend.config import Settings


def make(**kw):
    return Settings(_env_file=None, **kw)


def test_cors_comma_string_and_extra_env_keys(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("CORS_ORIGINS=http://a.com, http://b.com\nPOSTGRES_HOST=db\nTWILIO_AUTH_TOKEN=x\n")
    s = Settings(_env_file=str(env))
    assert s.cors_origins == ["http://a.com", "http://b.com"]


def test_database_url_forced_async():
    assert make(DATABASE_URL="postgresql://u:p@h:5432/d").DATABASE_URL == "postgresql+asyncpg://u:p@h:5432/d"
    assert make(DATABASE_URL="postgres://u:p@h/d").DATABASE_URL == "postgresql+asyncpg://u:p@h/d"
    assert make(DATABASE_URL="sqlite:///./x.db").DATABASE_URL == "sqlite+aiosqlite:///./x.db"


def test_production_requires_strong_jwt_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        make(APP_ENV="production")
    assert make(APP_ENV="production", JWT_SECRET_KEY="x" * 40).APP_ENV == "production"


def test_live_trading_needs_explicit_opt_in():
    with pytest.raises(ValidationError):
        make(TRADING_MODE="alpaca_live")
    assert make(TRADING_MODE="alpaca_live", ALLOW_LIVE_TRADING=True).TRADING_MODE == "alpaca_live"
    with pytest.raises(ValidationError):
        make(TRADING_MODE="yolo")
