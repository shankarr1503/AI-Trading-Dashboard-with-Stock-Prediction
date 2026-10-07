"""Database session and engine configuration."""
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from backend.config import settings

engine_kwargs: dict = {"echo": False}
if "sqlite" in settings.DATABASE_URL:
    # SQLite connections are cheap; not pooling them avoids sharing aiosqlite
    # connections across event loops (tests, the runner, alembic).
    engine_kwargs.update({"poolclass": NullPool, "connect_args": {"timeout": 30}})
else:
    engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True})

engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        # WAL lets the API read while the bot runner writes (and vice versa).
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (datetime.utcnow() is deprecated)."""
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """Normalise DB datetimes (SQLite returns naive values) to aware UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, commits on success, rolls back on error."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
