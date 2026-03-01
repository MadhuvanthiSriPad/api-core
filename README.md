# Contract Propagation Engine

**Autonomous breaking-change remediation across microservices, powered by Devin.**

When an API contract changes, every downstream service that depends on it becomes a ticking incident. Manual remediation means engineers coordinating across repos, Slack threads, and deploy queues — often taking days. This system detects the change, maps the blast radius from real telemetry, and dispatches Devin agents to fix every affected repo in parallel.

## What It Does

1. **Detects** — Diffs your OpenAPI spec against the last known snapshot. Classifies changes by severity (breaking, deprecation, additive).
2. **Maps** — Combines declared service dependencies with 7-day usage telemetry to identify every impacted service and route, with call-volume evidence.
3. **Dispatches** — Sends each affected repo to a dedicated Devin session with a targeted prompt containing the exact breaking changes, affected files, and test paths. Sessions run in dependency-ordered waves so upstream fixes land before downstream ones start.
4. **Monitors** — Polls session status, validates PRs against CI and protected-path guardrails, and fails closed on ambiguity. Every state transition is audit-logged.

## Architecture

```
openapi.yaml change
       |
   [differ] --> [classifier] --> [impact mapper]
                                       |
                              telemetry + service_map
                                       |
                              [bundle builder] --> [wave dispatcher]
                                                        |
                                               Devin sessions (parallel)
                                                        |
                                               [status poller] --> audit log
```

Three services:

| Service | Role |
|---------|------|
| **api-core** | FastAPI gateway + propagation engine. Hosts session tracking, analytics, usage telemetry, and the full contract change pipeline. |
| **billing-service** | Generates invoices from api-core session data. Token-level cost attribution per team. |
| **dashboard-service** | BFF proxy for the React frontend. Aggregates sessions, analytics, and contract status into dashboard views. |

## Why Devin (Not Copilot/Cursor)

This is not autocomplete. Each remediation session:

- **Runs 30-90 minutes autonomously** — clones the repo, investigates the impact, updates client code, fixes tests, iterates on failures, and opens a production-ready PR.
- **Requires multi-step reasoning** — understanding how a field removal cascades through HTTP clients, Pydantic schemas, test fixtures, and frontend types.
- **Has no human in the loop** — the agent must make judgment calls (default values for new required fields, backward-compatible response handling) and document assumptions in the PR.
- **Runs in parallel** — multiple Devin sessions work on different repos simultaneously, with wave ordering ensuring dependency correctness.

A copilot suggests the next line. Devin fixes the entire repo.

## Safety Model

- **CI gating**: PRs without passing CI are flagged, not merged. Unknown CI status after 5 polls fails closed.
- **Protected paths**: Changes to infra/, terraform/, .github/workflows/ trigger human review.
- **Path validation**: If changed files can't be verified against protected paths, the job requires human approval.
- **Audit trail**: Every job state transition (queued -> running -> pr_opened -> green/needs_human) is logged with timestamps and detail.
- **No auto-merge**: PRs are opened for review, never merged automatically.
- **Idempotency**: Duplicate dispatches are prevented via bundle hashing and Devin session idempotency keys.

## Quick Start

```bash
# Run the full pipeline (requires DEVIN_API_KEY)
python -m propagate

# Dry run — simulate without calling Devin
python -m propagate --dry-run

# Check status of in-progress jobs
python -m propagate.check_status

# Start the API server on the default api-core port
python scripts/run_dev_server.py

# Or, if you want raw uvicorn, restrict reload to project code
uvicorn src.main:app --host 127.0.0.1 --port 8001 --reload \
  --reload-dir src --reload-dir propagate --reload-dir scripts
```

## Configuration

All settings via environment variables with `API_CORE_` prefix:

| Variable | Purpose |
|----------|---------|
| `API_CORE_DATABASE_URL` | Database connection (default: SQLite for dev) |
| `API_CORE_API_KEY` | Service-to-service auth key (required in production) |
| `API_CORE_DEVIN_API_KEY` | Devin API authentication |
| `API_CORE_GITHUB_TOKEN` | GitHub API access for CI status checks |
| `API_CORE_DEVIN_SYNC_ENABLED` | Enable background session sync |

## Business Impact

Each breaking change across N services:

| Manual | With Propagation Engine |
|--------|----------------------|
| 2-4 eng-hours per service | ~15 min total (parallel) |
| Sequential PRs across repos | Simultaneous PRs, dependency-ordered |
| Risk of missed services | Telemetry-verified blast radius |
| Incident if any service missed | Fail-closed safety on every path |
