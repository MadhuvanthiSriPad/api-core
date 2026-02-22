"""Alembic async environment for api-core migrations."""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import Base

# Import all models so they register with Base.metadata
from src.models.agent_session import AgentSession  # noqa: F401
from src.models.token_usage import TokenUsage  # noqa: F401
from src.models.team import Team  # noqa: F401
from src.models.usage_request import UsageRequest  # noqa: F401
from src.models.contract_snapshot import ContractSnapshot  # noqa: F401
from src.models.contract_change import ContractChange  # noqa: F401
from src.models.impact_set import ImpactSet  # noqa: F401
from src.models.remediation_job import RemediationJob  # noqa: F401
from src.models.audit_log import AuditLog  # noqa: F401
from src.models.service_dependency import ServiceDependency  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Get database URL from env or config."""
    return os.getenv(
        "API_CORE_DATABASE_URL",
        config.get_main_option("sqlalchemy.url", "sqlite+aiosqlite:///./api_core.db"),
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL without connecting."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
