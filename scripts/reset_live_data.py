"""Clear seeded/demo records so the dashboard reflects only real runtime activity.

Usage:
    python scripts/reset_live_data.py                 # wipe all seeded/demo tables
    python scripts/reset_live_data.py --contracts-only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import async_session, init_db


CONTRACT_TABLES = [
    "audit_log",
    "remediation_jobs",
    "impact_sets",
    "contract_changes",
    "contract_snapshots",
]

ALL_RUNTIME_TABLES = CONTRACT_TABLES + [
    "usage_requests",
    "token_usage",
    "agent_sessions",
    "teams",
]


async def _count_rows(table: str) -> int:
    async with async_session() as db:
        result = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return int(result.scalar() or 0)


async def _delete_table(table: str) -> int:
    count = await _count_rows(table)
    async with async_session() as db:
        await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()
    return count


async def main(contracts_only: bool) -> None:
    await init_db()
    tables = CONTRACT_TABLES if contracts_only else ALL_RUNTIME_TABLES

    print("Resetting runtime data...")
    for table in tables:
        removed = await _delete_table(table)
        print(f"  {table}: removed {removed} row(s)")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clear seeded/demo rows from api-core")
    parser.add_argument(
        "--contracts-only",
        action="store_true",
        help="Only clear contract propagation tables; keep sessions/usage telemetry",
    )
    args = parser.parse_args()
    asyncio.run(main(contracts_only=args.contracts_only))
