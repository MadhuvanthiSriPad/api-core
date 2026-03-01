"""Generate realistic local traffic using only the real service names.

Populates:
- usage_requests via normal API calls with X-Caller-Service
- agent_sessions and token_usage via real session lifecycle endpoints

Does not touch:
- contract_changes
- remediation_jobs
- audit_logs
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


BASE_URL = "http://127.0.0.1:8001"
API_KEY = "demo-api-key"
REAL_CALLERS = ("billing-service", "dashboard-service", "notification-service")
MODELS = ("devin-default", "devin-fast", "devin-reasoning", "claude-3-5-sonnet")
AGENTS = (
    "deploy-assistant",
    "invoice-orchestrator",
    "contract-reviewer",
    "notification-worker",
    "audit-analyzer",
)
PRIORITIES = ("low", "medium", "high", "critical")


def _request(
    method: str,
    path: str,
    *,
    caller: str,
    body: dict | None = None,
    timeout: int = 20,
) -> dict | list | str | None:
    data = None
    headers = {
        "X-API-Key": API_KEY,
        "X-Caller-Service": caller,
    }
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        method=method,
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return None
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(raw)
            return raw
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def _token_event(scale: float) -> tuple[int, int, int]:
    input_tokens = int(random.randint(900, 4200) * scale)
    output_tokens = int(random.randint(300, 2600) * scale)
    cached_tokens = int(input_tokens * random.uniform(0.08, 0.3))
    return input_tokens, output_tokens, cached_tokens


def _create_session(team_id: str) -> str:
    payload = {
        "team_id": team_id,
        "agent_name": random.choice(AGENTS),
        "model": random.choices(MODELS, weights=[0.42, 0.24, 0.18, 0.16], k=1)[0],
        "priority": random.choices(PRIORITIES, weights=[0.2, 0.36, 0.32, 0.12], k=1)[0],
        "prompt": random.choice(
            [
                "Reconcile session usage totals against invoice rollups.",
                "Review billing impact from contract changes and summarize risk.",
                "Prepare notification payloads for recently completed sessions.",
                "Check dashboard latency regressions across the last release.",
            ]
        ),
    }
    created = _request("POST", "/api/v1/sessions", caller="billing-service", body=payload)
    assert isinstance(created, dict)
    return created["session_id"]


def _record_session_flow(session_id: str) -> None:
    event_count = random.randint(2, 5)
    scale = random.uniform(0.8, 1.8)
    for _ in range(event_count):
        inp, out, cached = _token_event(scale)
        qs = urllib.parse.urlencode(
            {
                "input_tokens": inp,
                "output_tokens": out,
                "cached_tokens": cached,
            }
        )
        _request("POST", f"/api/v1/sessions/{session_id}/tokens?{qs}", caller="billing-service")

    final_status = random.choices(
        ["completed", "failed", "cancelled"],
        weights=[0.82, 0.13, 0.05],
        k=1,
    )[0]
    ended_at = datetime.now(timezone.utc).isoformat()
    body = {"status": final_status, "ended_at": ended_at}
    if final_status == "failed":
        body["error_message"] = random.choice(
            [
                "Transient upstream timeout while reconciling invoices",
                "Notification worker lost auth token during callback delivery",
                "Context window overflow during contract review step",
            ]
        )
    _request("PATCH", f"/api/v1/sessions/{session_id}", caller="billing-service", body=body)


def _exercise_read_paths(session_ids: list[str], loops: int) -> None:
    for _ in range(loops):
        if session_ids and random.random() < 0.7:
            sid = random.choice(session_ids)
            _request("GET", f"/api/v1/sessions/{sid}", caller="notification-service")

        _request("GET", "/api/v1/sessions", caller="billing-service")
        _request("GET", "/api/v1/sessions/stats", caller="notification-service")
        _request("GET", "/api/v1/teams", caller="notification-service")

        # Dashboard-service is a real repo, but hidden from top-callers rankings.
        _request("GET", "/api/v1/contracts/changes", caller="dashboard-service")
        _request("GET", "/api/v1/contracts/service-graph", caller="dashboard-service")
        _request("GET", "/api/v1/usage/top-routes", caller="dashboard-service")
        _request("GET", "/api/v1/usage/top-callers", caller="dashboard-service")
        _request("GET", "/api/v1/usage/service-health", caller="dashboard-service")
        _request("GET", "/api/v1/usage/error-rates", caller="dashboard-service")
        _request("GET", "/api/v1/analytics/token-usage/daily?days=14", caller="dashboard-service")
        _request("GET", "/api/v1/analytics/cost-by-team?hours=336", caller="dashboard-service")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate realistic local API traffic")
    parser.add_argument("--sessions", type=int, default=36, help="number of new sessions to create")
    parser.add_argument("--reads", type=int, default=80, help="number of read-traffic loops")
    parser.add_argument("--sleep-ms", type=int, default=30, help="small pause between write flows")
    args = parser.parse_args()

    teams = _request("GET", "/api/v1/teams", caller="billing-service")
    if not isinstance(teams, list) or not teams:
        raise RuntimeError("No teams available from /api/v1/teams")
    team_ids = [t["id"] for t in teams if isinstance(t, dict) and "id" in t]

    session_ids: list[str] = []
    for _ in range(args.sessions):
        sid = _create_session(random.choice(team_ids))
        session_ids.append(sid)
        _record_session_flow(sid)
        time.sleep(max(args.sleep_ms, 0) / 1000.0)

    _exercise_read_paths(session_ids, loops=args.reads)

    print(
        json.dumps(
            {
                "created_sessions": len(session_ids),
                "read_loops": args.reads,
                "callers": list(REAL_CALLERS),
            }
        )
    )


if __name__ == "__main__":
    main()
