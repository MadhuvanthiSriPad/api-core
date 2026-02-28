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
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from propagate.check_status import check_jobs, TERMINAL_STATUSES
from propagate.devin_client import DevinClient

from src.database import async_session, init_db
from src.entities.contract_snapshot import ContractSnapshot
from src.entities.contract_change import ContractChange
from src.entities.impact_set import ImpactSet
from src.entities.remediation_job import RemediationJob, JobStatus


CONTRACT_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"

WAVE_POLL_INTERVAL = 30  # seconds between wave completion polls
WAVE_MAX_POLLS = 60      # max polls (30 min timeout)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        norm = value.strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
    return result


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


def _infer_patterns_from_files(changed_files: list[str]) -> list[str]:
    patterns: list[str] = []
    lowered = [path.lower() for path in changed_files]

    if any(
        token in path
        for path in lowered
        for token in ("client", "gateway", "http", "api/")
    ):
        patterns.append("updated API client callsites")
    if any(
        token in path
        for path in lowered
        for token in ("schema", "pydantic", "types", "dto")
    ):
        patterns.append("updated schema/type contracts")
    if any(
        token in path
        for path in lowered
        for token in ("route", "handler", "service")
    ):
        patterns.append("updated business logic adapters")
    if any(
        token in path
        for path in lowered
        for token in ("test", "spec", "fixture", "conftest")
    ):
        patterns.append("updated tests/fixtures for contract compatibility")

    return patterns


def _extract_fix_insights(session_payload: dict[str, Any]) -> dict[str, Any]:
    structured = session_payload.get("structured_output")
    if not isinstance(structured, dict):
        structured = {}

    changed_files: list[str] = []
    for key in ("changed_files", "files_changed", "modified_files"):
        changed_files.extend(_as_string_list(structured.get(key)))
        changed_files.extend(_as_string_list(session_payload.get(key)))
    changes = structured.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict):
                changed_files.extend(_as_string_list(change.get("files")))
                changed_files.extend(_as_string_list(change.get("changed_files")))
    changed_files = _dedupe_keep_order(changed_files)

    explicit_patterns: list[str] = []
    for key in ("patterns_used", "applied_patterns", "fix_patterns"):
        explicit_patterns.extend(_as_string_list(structured.get(key)))
        explicit_patterns.extend(_as_string_list(session_payload.get(key)))

    test_fixtures_changed: list[str] = []
    for key in ("test_fixtures_changed", "fixtures_changed"):
        test_fixtures_changed.extend(_as_string_list(structured.get(key)))
        test_fixtures_changed.extend(_as_string_list(session_payload.get(key)))
    if not test_fixtures_changed:
        test_fixtures_changed = [
            path
            for path in changed_files
            if any(token in path.lower() for token in ("/fixtures/", "\\fixtures\\", "fixture", "conftest.py"))
        ]
    test_fixtures_changed = _dedupe_keep_order(test_fixtures_changed)

    change_summary = ""
    for key in ("change_summary", "summary", "fix_summary", "result_summary"):
        value = structured.get(key)
        if isinstance(value, str) and value.strip():
            change_summary = value.strip()
            break
        value = session_payload.get(key)
        if isinstance(value, str) and value.strip():
            change_summary = value.strip()
            break

    patterns_used = _dedupe_keep_order(explicit_patterns + _infer_patterns_from_files(changed_files))

    return {
        "patterns_used": patterns_used,
        "test_fixtures_changed": test_fixtures_changed,
        "changed_files": changed_files,
        "change_summary": change_summary,
    }


async def _build_wave_context_payload(job_ids: list[int], wave_idx: int) -> dict[str, Any] | None:
    """Build structured context from completed wave outputs for the next wave."""
    if not job_ids:
        return None

    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(
            select(RemediationJob).where(RemediationJob.job_id.in_(job_ids))
        )
        finished_jobs = list(result.scalars().all())

    if not finished_jobs:
        return None

    client: DevinClient | None = None
    try:
        client = DevinClient()
    except Exception:
        client = None

    async def build_one(job: RemediationJob) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if client is not None and job.devin_run_id:
            try:
                payload = await client.get_session(job.devin_run_id)
            except Exception:
                payload = {}
        insights = _extract_fix_insights(payload)
        repo_name = job.target_repo.rstrip("/").split("/")[-1] or job.target_repo
        return {
            "repo": repo_name,
            "status": job.status,
            "pr_url": job.pr_url,
            "patterns_used": insights["patterns_used"],
            "test_fixtures_changed": insights["test_fixtures_changed"],
            "changed_files": insights["changed_files"],
            "change_summary": insights["change_summary"],
        }

    try:
        upstream_fix_summaries = list(await asyncio.gather(*(build_one(job) for job in finished_jobs)))
    finally:
        if client is not None:
            await client.close()

    notable_patterns = _dedupe_keep_order(
        [
            pattern
            for item in upstream_fix_summaries
            for pattern in item.get("patterns_used", [])
            if isinstance(pattern, str)
        ]
    )
    test_fixtures_changed = _dedupe_keep_order(
        [
            fixture
            for item in upstream_fix_summaries
            for fixture in item.get("test_fixtures_changed", [])
            if isinstance(fixture, str)
        ]
    )
    ci_green_prs = [
        item["pr_url"]
        for item in upstream_fix_summaries
        if item.get("status") == JobStatus.GREEN.value and isinstance(item.get("pr_url"), str) and item["pr_url"]
    ]

    status_parts = [
        f"{item['repo']}: {str(item.get('status', '')).upper()}" + (f" ({item['pr_url']})" if item.get("pr_url") else "")
        for item in upstream_fix_summaries
    ]
    summary_parts = [
        f"Wave {wave_idx} complete.",
        f"Upstream remediation status: {'; '.join(status_parts)}.",
    ]
    if notable_patterns:
        summary_parts.append(f"Patterns used upstream: {', '.join(notable_patterns)}.")
    if test_fixtures_changed:
        summary_parts.append(f"Test fixtures updated upstream: {', '.join(test_fixtures_changed)}.")
    summary_parts.append("Use these upstream outcomes as context before selecting your remediation strategy.")
    summary_text = " ".join(summary_parts)

    return {
        "source_wave_index": wave_idx,
        "upstream_fix_summaries": upstream_fix_summaries,
        "notable_patterns": notable_patterns,
        "test_fixtures_changed": test_fixtures_changed,
        "ci_green_prs": ci_green_prs,
        "summary_text": summary_text,
    }


async def _wait_for_wave_completion(job_ids: list[int], wave_idx: int) -> bool:
    """Poll until all jobs in the wave reach a terminal status.

    Returns True if all jobs completed, False on timeout.
    """
    from sqlalchemy import select

    for poll in range(WAVE_MAX_POLLS):
        await asyncio.sleep(WAVE_POLL_INTERVAL)
        try:
            await check_jobs()
        except Exception as e:
            print(f"  Wave {wave_idx} poll error: {e}")

        async with async_session() as db:
            result = await db.execute(
                select(RemediationJob).where(
                    RemediationJob.job_id.in_(job_ids),
                    RemediationJob.status.notin_(TERMINAL_STATUSES),
                    RemediationJob.devin_run_id.isnot(None),
                )
            )
            pending = list(result.scalars().all())
            if not pending:
                print(f"  Wave {wave_idx} complete — all jobs reached terminal status")
                return True
            print(f"  Wave {wave_idx} poll {poll + 1}: {len(pending)} job(s) still running")

    print(f"  Wave {wave_idx} timed out after {WAVE_MAX_POLLS} polls")
    return False


async def _send_context_to_wave(
    wave_jobs: list[RemediationJob],
    wave_idx: int,
    context_payload: dict[str, Any] | None,
) -> None:
    """Send prior-wave context to each newly-dispatched job in this wave."""
    if not context_payload:
        return

    session_ids = [job.devin_run_id for job in wave_jobs if job.devin_run_id]
    if not session_ids:
        return

    source_wave_index = context_payload.get("source_wave_index")
    summary_text = context_payload.get("summary_text")
    if not isinstance(summary_text, str) or not summary_text.strip():
        source_label = source_wave_index if isinstance(source_wave_index, int) else "previous"
        summary_text = f"Wave {source_label} complete. Use upstream remediation outcomes as context."

    wave_context = {
        "type": "wave-context",
        "wave_index": wave_idx,
        "source_wave_index": source_wave_index,
        "upstream_fix_summaries": context_payload.get("upstream_fix_summaries", []),
        "notable_patterns": context_payload.get("notable_patterns", []),
        "test_fixtures_changed": context_payload.get("test_fixtures_changed", []),
        "ci_green_prs": context_payload.get("ci_green_prs", []),
    }

    client = DevinClient()
    print(f"  Sending prior-wave context to wave {wave_idx} ({len(session_ids)} session(s))...")

    async def send_one(session_id: str) -> None:
        try:
            await client.send_message(
                session_id,
                summary_text,
                wave_context=wave_context,
            )
        except Exception as e:
            print(f"    Context message failed for {session_id}: {e}")

    try:
        await asyncio.gather(*(send_one(session_id) for session_id in session_ids))
    finally:
        await client.close()


async def main(dry_run: bool = False, no_wait: bool = False, ci: bool = False):
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
            if ci:
                # In CI mode, use an empty spec as baseline so first PR always produces a diff
                print("No previous snapshot found (CI mode). Using empty baseline for diff.")
                old_spec = {"openapi": "3.1.0", "info": {}, "paths": {}}
                # Store the empty baseline
                empty_content = json.dumps(old_spec)
                empty_hash = hashlib.sha256(empty_content.encode()).hexdigest()[:16]
                baseline = ContractSnapshot(
                    version_hash=empty_hash,
                    content=empty_content,
                    git_sha=os.getenv("GITHUB_SHA", ""),
                )
                db.add(baseline)
                await db.flush()
            else:
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

        if old_snapshot is not None:
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

        # Step 3: Impact mapping — service map is the authoritative source.
        # Services that declare depends_on api-core are always impacted.
        # Telemetry enriches call counts but does not gate inclusion.
        print("\n--- STEP 3: Impact mapping ---")
        svc_map = load_service_map()
        declared_dependents = {
            name for name, info in svc_map.items()
            if "api-core" in info.depends_on
        }
        impacts = await compute_impact_sets(
            db, classified.changed_routes, declared_dependents
        )
        print(f"  Found {len(impacts)} impacted caller(s):")
        for imp in impacts:
            calls_str = f"{imp.calls_last_7d} calls/7d" if imp.calls_last_7d else "declared dependent"
            print(f"    {imp.caller_service} → {imp.route_template} ({calls_str})")

            # Store impact set
            impact_row = ImpactSet(
                change_id=change.id,
                route_template=imp.route_template,
                method=imp.method,
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
        await db.flush()
        await db.commit()  # release SQLite write lock before remediation_jobs inserts


        # Step 4: Build dependency graph from already-loaded service map
        print("\n--- STEP 4: Loading service map & dependency graph ---")
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
            sim_results = []
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
                    guardrail_paths = sorted(set(b.client_paths + b.test_paths + b.frontend_paths))
                    violations = guardrails.validate_paths(guardrail_paths)
                    if violations:
                        print(f"    [{b.target_service}] WOULD BE BLOCKED: {violations}")
                        # Store blocked simulation result
                        sim_job = RemediationJob(
                            change_id=change.id,
                            target_repo=b.target_repo,
                            status="needs_human",
                            bundle_hash=b.bundle_hash,
                            error_summary=f"Guardrail violation: {'; '.join(violations)}",
                            is_dry_run=True,
                        )
                        db.add(sim_job)
                        sim_results.append((b.target_service, "NEEDS_HUMAN", 0, "guardrail blocked"))
                    else:
                        print(f"    [{b.target_service}] → {b.target_repo}")
                        print(f"      Prompt length: {len(b.prompt)} chars")
                        print(f"      Affected routes: {b.affected_routes}")

            # Simulate realistic randomized lifecycle
            print("\n--- STEP 6b: Simulated check_status lifecycle ---")
            for b in bundles:
                guardrail_paths = sorted(set(b.client_paths + b.test_paths + b.frontend_paths))
                violations = guardrails.validate_paths(guardrail_paths)
                if violations:
                    continue

                # Randomized terminal state: GREEN 60%, CI_FAILED 20%, NEEDS_HUMAN 20%
                roll = random.random()
                if roll < 0.6:
                    terminal = "GREEN"
                    detail = "CI passed, PR ready for review"
                elif roll < 0.8:
                    terminal = "CI_FAILED"
                    detail = "CI failed: test assertions broke"
                else:
                    terminal = "NEEDS_HUMAN"
                    detail = "Devin session blocked, requires human review"

                duration_min = random.randint(15, 90)

                print(f"  [{b.target_service}] QUEUED -> RUNNING -> PR_OPENED -> {terminal} ({duration_min}m)")
                print(f"    {detail}")

                sim_job = RemediationJob(
                    change_id=change.id,
                    target_repo=b.target_repo,
                    status=terminal.lower(),
                    bundle_hash=b.bundle_hash,
                    is_dry_run=True,
                    error_summary=detail if terminal != "GREEN" else None,
                )
                db.add(sim_job)
                sim_results.append((b.target_service, terminal, duration_min, detail))

            await db.flush()

            # Print summary table
            print(f"\n{'='*60}")
            print("DRY-RUN SIMULATION SUMMARY")
            print(f"{'='*60}")
            print(f"  {'Service':<25} {'Status':<15} {'Time':<8} Detail")
            print(f"  {'-'*25} {'-'*15} {'-'*8} {'-'*30}")
            for svc, status, mins, detail in sim_results:
                time_str = f"{mins}m" if mins else "—"
                print(f"  {svc:<25} {status:<15} {time_str:<8} {detail[:40]}")

            green = sum(1 for _, s, _, _ in sim_results if s == "GREEN")
            failed = sum(1 for _, s, _, _ in sim_results if s == "CI_FAILED")
            human = sum(1 for _, s, _, _ in sim_results if s == "NEEDS_HUMAN")
            print(f"\n  Totals: {green} GREEN, {failed} CI_FAILED, {human} NEEDS_HUMAN")
            print(f"\n[DRY-RUN] Pipeline complete. {len(bundles)} bundle(s) simulated.")
        else:
            next_wave_context: dict[str, Any] | None = None
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
                    wave_bundles, guardrails, change.id, wave_context_payload=next_wave_context
                )
                jobs.extend(wave_jobs)

                # After upstream wave completion, send context to newly dispatched wave.
                await _send_context_to_wave(
                    wave_jobs=wave_jobs,
                    wave_idx=wave_idx,
                    context_payload=next_wave_context,
                )

                # Wait for wave completion before proceeding (including the final wave)
                if not no_wait:
                    dispatched_ids = [j.job_id for j in wave_jobs if j.devin_run_id]
                    if dispatched_ids:
                        next_label = f"wave {wave_idx + 1}" if wave_idx < len(waves) - 1 else "snapshot advancement"
                        print(f"\n  Waiting for wave {wave_idx} to complete before {next_label}...")
                        await _wait_for_wave_completion(dispatched_ids, wave_idx)
                        if wave_idx < len(waves) - 1:
                            next_wave_context = await _build_wave_context_payload(
                                dispatched_ids, wave_idx
                            )

        # Step 7: Decide whether snapshot can advance.
        should_store_snapshot = True
        fail_pipeline = False

        if dry_run:
            should_store_snapshot = False
            print("\n[DRY-RUN] Snapshot not advanced (simulation mode).")
        elif no_wait:
            should_store_snapshot = False
            print("\n[NO-WAIT] Snapshot not advanced because jobs may still be running.")
        elif jobs:
            job_ids = [j.job_id for j in jobs]
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id.in_(job_ids))
            )
            fresh_jobs = result.scalars().all()
            unresolved = [j for j in fresh_jobs if j.status in {"ci_failed", "needs_human"}]
            if unresolved:
                should_store_snapshot = False
                fail_pipeline = True
                print(f"\nWARNING: {len(unresolved)} job(s) in unresolved terminal state — snapshot NOT advanced.")
                for j in unresolved:
                    print(f"  [{j.target_repo}] status={j.status}: {j.error_summary or ''}")
                print("Resolve these jobs before re-running. The same contract hash will re-trigger on next push.\n")

        if not should_store_snapshot:
            await db.commit()  # persist change/impact_sets/job records
            if fail_pipeline:
                sys.exit(1)
            return

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
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip wave completion gating (fire-and-forget mode)",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: use empty baseline if no snapshot exists (ensures first PR always diffs)",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, no_wait=args.no_wait, ci=args.ci))


if __name__ == "__main__":
    cli()
