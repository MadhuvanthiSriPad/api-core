"""Entry point: python -m propagate

Detects contract changes, maps impact via usage telemetry, and dispatches
Devin to fix affected consumer repos.

Usage:
    python -m propagate              # Full run (requires DEVIN_API_KEY)
    python -m propagate --dry-run    # Simulate full pipeline without Devin API
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

# Ensure the api-core src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from propagate.differ import diff_contracts, load_contract
from propagate.classifier import classify_changes
from propagate.impact import compute_impact_sets
from propagate.service_map import load_service_map
from propagate.bundle import build_fix_bundles
from propagate.dispatcher import dispatch_remediation_jobs
from propagate.guardrails import load_guardrails
from propagate.dependency_graph import build_dependency_graph_from_service_map

from src.database import async_session, init_db
from src.models.contract_snapshot import ContractSnapshot
from src.models.contract_change import ContractChange
from src.models.impact_set import ImpactSet


CONTRACT_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"


async def main(dry_run: bool = False):
    print("=" * 60)
    print("CONTRACT CHANGE PROPAGATION ENGINE")
    if dry_run:
        print("  ** DRY-RUN MODE — no Devin sessions will be created **")
    print("=" * 60)

    # Load guardrails and print config
    guardrails = load_guardrails()
    guardrails.print_config()

    # Initialize DB
    await init_db()

    # Load new contract from disk
    if not CONTRACT_PATH.exists():
        print(f"ERROR: Contract file not found at {CONTRACT_PATH}")
        sys.exit(1)

    new_content = CONTRACT_PATH.read_text()
    new_spec = yaml.safe_load(new_content)
    new_hash = hashlib.sha256(new_content.encode()).hexdigest()[:16]
    print(f"\nNew contract hash: {new_hash}")

    # Load old contract from DB (most recent snapshot)
    async with async_session() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(ContractSnapshot)
            .order_by(ContractSnapshot.captured_at.desc())
            .limit(1)
        )
        old_snapshot = result.scalar_one_or_none()

        if old_snapshot is None:
            print("No previous contract snapshot found. Storing current as baseline.")
            snapshot = ContractSnapshot(
                version_hash=new_hash,
                content=new_content,
                git_sha=os.getenv("GITHUB_SHA", ""),
            )
            db.add(snapshot)
            await db.commit()
            print("Baseline stored. No diff to propagate.")
            return

        if old_snapshot.version_hash == new_hash:
            print("Contract unchanged. Nothing to propagate.")
            return

        old_spec = yaml.safe_load(old_snapshot.content)
        print(f"Old contract hash: {old_snapshot.version_hash}")

        # Step 1: Diff contracts
        print("\n--- STEP 1: Diffing contracts ---")
        diffs = diff_contracts(old_spec, new_spec)
        print(f"  Found {len(diffs)} diff(s)")
        for d in diffs:
            print(f"    {d.method.upper()} {d.path} / {d.field}: {d.diff_type}")

        if not diffs:
            print("No meaningful diffs found. Updating snapshot.")
            snapshot = ContractSnapshot(
                version_hash=new_hash,
                content=new_content,
                git_sha=os.getenv("GITHUB_SHA", ""),
            )
            db.add(snapshot)
            await db.commit()
            return

        # Step 2: Classify changes
        print("\n--- STEP 2: Classifying changes ---")
        classified = classify_changes(diffs)
        print(f"  Breaking: {classified.is_breaking}")
        print(f"  Severity: {classified.severity}")
        print(f"  Summary:  {classified.summary}")
        print(f"  Routes:   {classified.changed_routes}")

        # Store the contract change
        change = ContractChange(
            base_ref=old_snapshot.version_hash,
            head_ref=new_hash,
            is_breaking=classified.is_breaking,
            severity=classified.severity,
            summary_json=json.dumps({"summary": classified.summary}),
            changed_routes_json=json.dumps(classified.changed_routes),
            changed_fields_json=json.dumps(classified.changed_fields),
        )
        db.add(change)
        await db.flush()
        print(f"  Stored as change_id={change.id}")

        # Step 3: Impact mapping via usage telemetry
        print("\n--- STEP 3: Impact mapping (last 7 days usage) ---")
        impacts = await compute_impact_sets(db, classified.changed_routes)
        print(f"  Found {len(impacts)} impacted caller(s):")
        for imp in impacts:
            print(f"    {imp.caller_service} → {imp.route_template} ({imp.calls_last_7d} calls)")

            # Store impact set
            impact_row = ImpactSet(
                change_id=change.id,
                route_template=imp.route_template,
                caller_service=imp.caller_service,
                calls_last_7d=imp.calls_last_7d,
                confidence="high",
            )
            db.add(impact_row)

        await db.flush()

        if not impacts:
            print("  No impacted services found. Updating snapshot.")
            snapshot = ContractSnapshot(
                version_hash=new_hash,
                content=new_content,
                git_sha=os.getenv("GITHUB_SHA", ""),
            )
            db.add(snapshot)
            await db.commit()
            return

        # Step 4: Load service map and build dependency graph
        print("\n--- STEP 4: Loading service map & dependency graph ---")
        svc_map = load_service_map()
        for name, info in svc_map.items():
            print(f"  {name} → {info.repo} (depends_on: {info.depends_on})")

        dep_graph = build_dependency_graph_from_service_map(svc_map)
        waves = dep_graph.topological_sort()
        print(f"  Dependency waves: {waves}")

        # Step 5: Build fix bundles
        print("\n--- STEP 5: Building fix bundles ---")
        bundles = build_fix_bundles(classified, impacts, svc_map)
        for b in bundles:
            print(f"  [{b.target_service}] {b.target_repo}")
            print(f"    Routes: {b.affected_routes}")
            print(f"    Calls (7d): {b.call_count_7d}")
            print(f"    Bundle hash: {b.bundle_hash}")

        # Step 6: Dispatch Devin jobs in dependency-aware waves
        print("\n--- STEP 6: Dispatching Devin jobs (wave-ordered) ---")
        jobs = []
        bundle_by_service = {b.target_service: b for b in bundles}

        if dry_run:
            print("  [DRY-RUN] Simulating dispatch — no API calls will be made")
            for wave_idx, wave_services in enumerate(waves):
                wave_bundles = [
                    bundle_by_service[svc]
                    for svc in wave_services
                    if svc in bundle_by_service
                ]
                if not wave_bundles:
                    continue
                print(f"\n  Wave {wave_idx}: {[b.target_service for b in wave_bundles]}")
                for b in wave_bundles:
                    violations = guardrails.validate_paths(b.client_paths)
                    if violations:
                        print(f"    [{b.target_service}] WOULD BE BLOCKED: {violations}")
                    else:
                        print(f"    [{b.target_service}] → {b.target_repo}")
                        print(f"      Prompt length: {len(b.prompt)} chars")
                        print(f"      Affected routes: {b.affected_routes}")
                        print(f"      Total calls (7d): {b.call_count_7d}")

            # Simulate check_status results
            print("\n--- STEP 6b: Simulated check_status results ---")
            statuses = ["green", "pr_opened", "ci_failed", "needs_human"]
            for i, b in enumerate(bundles):
                sim_status = statuses[i % len(statuses)]
                merge_ok, merge_reason = guardrails.check_can_merge(sim_status == "green")
                print(f"  [{b.target_service}] status={sim_status} | merge: {merge_reason}")

            print(f"\n[DRY-RUN] Pipeline complete. {len(bundles)} bundle(s) would be dispatched.")
        else:
            for wave_idx, wave_services in enumerate(waves):
                wave_bundles = [
                    bundle_by_service[svc]
                    for svc in wave_services
                    if svc in bundle_by_service
                ]
                if not wave_bundles:
                    continue
                print(f"\n  Wave {wave_idx}: {[b.target_service for b in wave_bundles]}")
                wave_jobs = await dispatch_remediation_jobs(
                    db, wave_bundles, guardrails, change.id
                )
                jobs.extend(wave_jobs)

        # Step 7: Store new snapshot
        snapshot = ContractSnapshot(
            version_hash=new_hash,
            content=new_content,
            git_sha=os.getenv("GITHUB_SHA", ""),
        )
        db.add(snapshot)
        await db.commit()

        print(f"\nNew contract snapshot stored: {new_hash}")
        if dry_run:
            print("Dry run complete. No Devin sessions were created.")
        else:
            print(f"Propagation complete. {len(jobs)} job(s) dispatched.")


def cli():
    parser = argparse.ArgumentParser(
        description="Contract Change Propagation Engine"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the full pipeline without calling the Devin API",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))


if __name__ == "__main__":
    cli()
