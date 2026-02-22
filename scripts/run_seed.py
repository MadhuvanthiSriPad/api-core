"""Seed the database with realistic demo data.

Usage:
    python scripts/run_seed.py          # seed all data
    python scripts/run_seed.py --reset  # drop and recreate tables first
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import async_session, init_db, engine, Base
from src.seed import seed_data


async def main(reset: bool = False) -> None:
    if reset:
        print("Dropping all tables...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    print("Initializing database...")
    await init_db()

    async with async_session() as db:
        print("Seeding data...")
        await seed_data(db)

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed demo data")
    parser.add_argument(
        "--reset", action="store_true", help="Drop and recreate tables before seeding"
    )
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))
