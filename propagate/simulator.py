"""Pre-Merge Contract Simulation Engine.

Simulates the downstream blast radius of contract changes before dispatch.
Produces per-service risk scores and optionally dispatches Devin for
deep pre-merge analysis of high-risk consumers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from propagate.differ import ContractDiff
from propagate.service_map import ServiceInfo

logger = logging.getLogger(__name__)

# Weights per breaking-issue type (sum clamped to 1.0)
_ISSUE_WEIGHTS: dict[str, float] = {
    "field_removed": 0.30,
    "field_type_changed": 0.25,
    "enum_values_removed": 0.20,
    "nullability_shift": 0.15,
    "field_added_required": 0.30,
    "field_optional_to_required": 0.25,
    "operation_removed": 0.35,
    "parameter_added_required": 0.25,
    "parameter_removed": 0.20,
    "nested_field_removed": 0.20,
    "nested_field_type_changed": 0.15,
    "response_structure_changed": 0.20,
    "content_type_changed": 0.15,
    "security_changed": 0.15,
}


@dataclass
class BreakingIssue:
    """A single predicted compatibility issue for a downstream service."""
    diff_type: str
    path: str
    method: str
    field: str
    detail: str
    weight: float


@dataclass
class SimulationResult:
    """Blast-radius prediction for one downstream service."""
    service: str
    risk_score: float = 0.0
    risk_level: str = "safe"  # "high" | "medium" | "safe"
    breaking_issues: list[BreakingIssue] = field(default_factory=list)
    fields_affected: int = 0
    routes_affected: int = 0
    devin_analysis_id: str | None = None


def _risk_level(score: float) -> str:
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "safe"


def _extract_field_name(field_path: str) -> str:
    """Extract the leaf field name from a dotted path like 'request.body.priority'."""
    return field_path.rsplit(".", 1)[-1] if "." in field_path else field_path


def _diff_affects_service(diff: ContractDiff, service_name: str, info: ServiceInfo) -> BreakingIssue | None:
    """Check if a single diff likely affects a service. Returns an issue or None."""
    weight = _ISSUE_WEIGHTS.get(diff.diff_type, 0.10)

    field_name = _extract_field_name(diff.field)
    route = f"{diff.method.upper()} {diff.path}"

    # Map diff_type to a human-readable detail
    detail_map = {
        "field_removed": f"Field '{field_name}' was removed from {diff.path}",
        "field_type_changed": f"Field '{field_name}' type changed: {diff.old_value} -> {diff.new_value}",
        "enum_values_removed": f"Enum values removed from '{field_name}': {diff.old_value} -> {diff.new_value}",
        "field_added_required": f"New required field '{field_name}' added to {diff.path}",
        "field_optional_to_required": f"Field '{field_name}' promoted from optional to required",
        "operation_removed": f"Operation {route} was removed entirely",
        "parameter_added_required": f"New required parameter '{field_name}' added to {route}",
        "parameter_removed": f"Parameter '{field_name}' removed from {route}",
        "nested_field_removed": f"Nested field '{field_name}' removed from {diff.path}",
        "nested_field_type_changed": f"Nested field '{field_name}' type changed",
        "response_structure_changed": f"Response structure changed for {route}",
        "content_type_changed": f"Content type changed for {route}",
        "security_changed": f"Security requirements changed for {route}",
    }

    detail = detail_map.get(diff.diff_type, f"{diff.diff_type} on {route}: {field_name}")

    return BreakingIssue(
        diff_type=diff.diff_type,
        path=diff.path,
        method=diff.method,
        field=diff.field,
        detail=detail,
        weight=weight,
    )


def simulate_contract_changes(
    diffs: list[ContractDiff],
    service_map: dict[str, ServiceInfo],
) -> list[SimulationResult]:
    """Run static compatibility simulation for all downstream services.

    For each service that depends on api-core, evaluates every diff and
    produces a risk score + list of predicted breaking issues.
    """
    results: list[SimulationResult] = []

    # Only simulate for services that depend on api-core
    dependent_services = {
        name: info
        for name, info in service_map.items()
        if "api-core" in info.depends_on
    }

    if not dependent_services:
        logger.info("No downstream dependents found — skipping simulation")
        return results

    # Only consider breaking diff types (skip additive/safe changes)
    breaking_types = set(_ISSUE_WEIGHTS.keys())

    for svc_name, svc_info in sorted(dependent_services.items()):
        issues: list[BreakingIssue] = []
        affected_fields: set[str] = set()
        affected_routes: set[str] = set()

        for diff in diffs:
            if diff.diff_type not in breaking_types:
                continue

            issue = _diff_affects_service(diff, svc_name, svc_info)
            if issue:
                issues.append(issue)
                affected_fields.add(diff.field)
                affected_routes.add(f"{diff.method.upper()} {diff.path}")

        # Compute risk score: sum of weights, clamped to 1.0
        raw_score = sum(issue.weight for issue in issues)
        risk_score = min(raw_score, 1.0)

        results.append(SimulationResult(
            service=svc_name,
            risk_score=round(risk_score, 3),
            risk_level=_risk_level(risk_score),
            breaking_issues=issues,
            fields_affected=len(affected_fields),
            routes_affected=len(affected_routes),
        ))

    # Sort by risk score descending (highest risk first)
    results.sort(key=lambda r: r.risk_score, reverse=True)
    return results


def build_devin_analysis_prompt(
    service_name: str,
    service_info: ServiceInfo,
    diffs: list[ContractDiff],
    simulation: SimulationResult,
) -> str:
    """Build a Devin prompt for pre-merge compatibility analysis."""
    issues_text = "\n".join(
        f"  - [{issue.diff_type}] {issue.detail}"
        for issue in simulation.breaking_issues
    )

    client_paths = ", ".join(service_info.client_paths) or "unknown"
    test_paths = ", ".join(service_info.test_paths) or "unknown"

    return f"""## Pre-Merge Contract Compatibility Analysis

You are analyzing **{service_name}** for compatibility with upcoming API contract changes.

### Predicted Breaking Changes ({len(simulation.breaking_issues)} issues, risk score: {simulation.risk_score})
{issues_text}

### Your Task
1. Clone the {service_name} repository
2. Check these client files for usage of affected fields/routes: {client_paths}
3. Check these test files for assertions against changed response shapes: {test_paths}
4. For each predicted issue above, determine:
   - Is the field/route actually used in this service? (true/false)
   - If used, what code locations reference it?
   - What is the likely fix complexity? (trivial/moderate/complex)

### Output Format
Respond with structured JSON:
```json
{{
  "service": "{service_name}",
  "confirmed_issues": [
    {{
      "diff_type": "field_removed",
      "field": "field_name",
      "actually_used": true,
      "code_locations": ["src/clients/gateway.py:42"],
      "fix_complexity": "trivial"
    }}
  ],
  "false_positives": ["field_name_not_used"],
  "additional_risks": ["any risks not caught by static analysis"],
  "overall_assessment": "brief summary"
}}
```

### Constraints
- Do NOT make any code changes or open PRs
- This is a READ-ONLY analysis task
- Focus on accuracy over speed
"""


async def request_devin_analysis(
    service_name: str,
    service_info: ServiceInfo,
    diffs: list[ContractDiff],
    simulation: SimulationResult,
    change_id: int,
) -> str | None:
    """Dispatch a Devin session for pre-merge analysis of a high-risk service.

    Returns the Devin session ID or None if dispatch fails.
    """
    from propagate.devin_client import DevinClient

    prompt = build_devin_analysis_prompt(service_name, service_info, diffs, simulation)

    try:
        client = DevinClient()
        result = await client.create_session(
            prompt=prompt,
            idempotency_key=f"sim-{change_id}-{service_name}",
        )
        session_id = result.get("session_id")
        logger.info(
            "Devin pre-merge analysis dispatched for %s: session=%s",
            service_name, session_id,
        )
        await client.close()
        return session_id
    except Exception as e:
        logger.warning("Failed to dispatch Devin analysis for %s: %s", service_name, e)
        return None


def format_blast_radius_table(results: list[SimulationResult]) -> str:
    """Format simulation results as a printable summary table."""
    if not results:
        return "  No downstream services affected."

    lines = [
        f"\n{'='*70}",
        "PRE-MERGE BLAST RADIUS SIMULATION",
        f"{'='*70}",
        f"  {'Service':<25} {'Risk':<8} {'Score':<8} {'Issues':<8} {'Routes':<8} {'Fields'}",
        f"  {'-'*25} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}",
    ]

    for r in results:
        risk_display = r.risk_level.upper()
        lines.append(
            f"  {r.service:<25} {risk_display:<8} {r.risk_score:<8.3f} "
            f"{len(r.breaking_issues):<8} {r.routes_affected:<8} {r.fields_affected}"
        )

    high = sum(1 for r in results if r.risk_level == "high")
    medium = sum(1 for r in results if r.risk_level == "medium")
    safe = sum(1 for r in results if r.risk_level == "safe")
    total = len(results)

    lines.append(f"\n  Predicted Impact: {total} services — {high} high risk, {medium} medium, {safe} safe")
    lines.append(f"  Mode: diff-aware + dependency-aware (no code checkout required)")

    if high > 0:
        lines.append(f"  Wave strategy: high-risk services dispatched first")
        lines.append(f"  Optional: use the dashboard verify button to add code-aware Devin verification for high-risk services")

    return "\n".join(lines)


def simulation_results_to_dicts(results: list[SimulationResult]) -> list[dict[str, Any]]:
    """Convert simulation results to JSON-serializable dicts for DB storage."""
    return [
        {
            "service": r.service,
            "risk_score": r.risk_score,
            "risk_level": r.risk_level,
            "breaking_issues": [
                {
                    "diff_type": issue.diff_type,
                    "path": issue.path,
                    "method": issue.method,
                    "field": issue.field,
                    "detail": issue.detail,
                    "weight": issue.weight,
                }
                for issue in r.breaking_issues
            ],
            "fields_affected": r.fields_affected,
            "routes_affected": r.routes_affected,
            "devin_analysis_id": r.devin_analysis_id,
        }
        for r in results
    ]
