#!/usr/bin/env python3
"""Seed realistic telemetry data for demo purposes.

Generates 7 days of HTTP request logs showing billing-service and
dashboard-service calling various endpoints on api-core.
"""

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import async_session, init_db
from src.models.usage_request import UsageRequest


async def seed_telemetry():
    """Generate realistic telemetry data for the last 7 days."""
    print("=" * 60)
    print("SEEDING TELEMETRY DATA")
    print("=" * 60)

    await init_db()

    # Define realistic call patterns
    patterns = [
        # billing-service heavily uses sessions endpoint for invoice generation
        {
            "caller": "billing-service",
            "route": "/api/v1/sessions",
            "method": "POST",
            "daily_calls": (45, 55),  # 45-55 calls per day
            "status": 201,
            "duration": (80, 150),
        },
        {
            "caller": "billing-service",
            "route": "/api/v1/sessions",
            "method": "GET",
            "daily_calls": (20, 30),
            "status": 200,
            "duration": (40, 90),
        },
        {
            "caller": "billing-service",
            "route": "/api/v1/analytics/cost-by-team",
            "method": "GET",
            "daily_calls": (10, 15),
            "status": 200,
            "duration": (120, 200),
        },

        # dashboard-service uses sessions for UI
        {
            "caller": "dashboard-service",
            "route": "/api/v1/sessions",
            "method": "POST",
            "daily_calls": (10, 15),
            "status": 201,
            "duration": (70, 130),
        },
        {
            "caller": "dashboard-service",
            "route": "/api/v1/sessions",
            "method": "GET",
            "daily_calls": (35, 45),
            "status": 200,
            "duration": (50, 100),
        },
        {
            "caller": "dashboard-service",
            "route": "/api/v1/sessions/stats",
            "method": "GET",
            "daily_calls": (15, 20),
            "status": 200,
            "duration": (90, 150),
        },
        {
            "caller": "dashboard-service",
            "route": "/api/v1/teams",
            "method": "GET",
            "daily_calls": (8, 12),
            "status": 200,
            "duration": (30, 70),
        },
        {
            "caller": "dashboard-service",
            "route": "/api/v1/analytics/token-usage/daily",
            "method": "GET",
            "daily_calls": (5, 10),
            "status": 200,
            "duration": (110, 180),
        },
    ]

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    records = []
    total_count = 0

    for pattern in patterns:
        # Generate requests for each day
        for day_offset in range(7):
            day_start = cutoff + timedelta(days=day_offset)
            calls_today = random.randint(*pattern["daily_calls"])

            for _ in range(calls_today):
                # Random time within the day
                random_seconds = random.randint(0, 86400 - 1)
                ts = day_start + timedelta(seconds=random_seconds)

                # Random duration
                duration = random.uniform(*pattern["duration"])

                # Occasional failures (2% of requests)
                status = pattern["status"]
                if random.random() < 0.02:
                    status = random.choice([400, 500, 503])

                records.append(UsageRequest(
                    caller_service=pattern["caller"],
                    route_template=pattern["route"],
                    method=pattern["method"],
                    status_code=status,
                    duration_ms=round(duration, 2),
                    ts=ts,
                ))
                total_count += 1

    # Insert in batches
    async with async_session() as db:
        batch_size = 100
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            db.add_all(batch)
            await db.flush()
            print(f"  Inserted {min(i + batch_size, len(records))}/{len(records)} records...")

        await db.commit()

    print(f"\n✓ Successfully seeded {total_count} telemetry records")
    print("\nBreakdown by service:")

    async with async_session() as db:
        from sqlalchemy import select, func

        result = await db.execute(
            select(
                UsageRequest.caller_service,
                UsageRequest.route_template,
                UsageRequest.method,
                func.count(UsageRequest.id).label("count"),
            )
            .where(UsageRequest.ts >= cutoff)
            .group_by(
                UsageRequest.caller_service,
                UsageRequest.route_template,
                UsageRequest.method,
            )
            .order_by(UsageRequest.caller_service, func.count(UsageRequest.id).desc())
        )

        current_service = None
        for row in result.all():
            if row.caller_service != current_service:
                current_service = row.caller_service
                print(f"\n  {current_service}:")
            print(f"    {row.method:6} {row.route_template:40} → {row.count:3} calls")

    print("\n" + "=" * 60)
    print("Telemetry seeding complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(seed_telemetry())
