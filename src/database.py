"""Database connection and session management."""

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from src.config import settings

logger = logging.getLogger(__name__)


def _ensure_sqlite_directory(database_url: str) -> None:
    """Ensure parent directory exists for file-backed sqlite URLs."""
    if not database_url.startswith("sqlite+aiosqlite:///"):
        return

    sqlite_path = database_url.removeprefix("sqlite+aiosqlite:///")

    # Ignore in-memory sqlite URLs.
    if sqlite_path in {"", ":memory:"}:
        return

    db_file = Path(sqlite_path)
    db_parent = db_file.parent
    if db_parent and str(db_parent) != ".":
        db_parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_directory(settings.database_url)

if "sqlite" in settings.database_url and "mode=memory" not in settings.database_url:
    logger.warning(
        "SQLite detected — not suitable for concurrent writes. "
        "Set API_CORE_DATABASE_URL to a Postgres URL for production."
    )

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    if "sqlite" in settings.database_url:
        # For SQLite (tests, dev), use create_all for fast setup
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    else:
        # For production (Postgres), run Alembic migrations
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")


async def close_db() -> None:
    await engine.dispose()
