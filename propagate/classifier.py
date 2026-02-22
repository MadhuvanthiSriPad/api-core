"""Classify contract diffs as breaking/additive and assign severity."""

from __future__ import annotations

import json
from dataclasses import dataclass

from propagate.differ import ContractDiff


BREAKING_DIFF_TYPES = {
    "field_added_required",
    "field_optional_to_required",
    "field_removed",
    "field_type_changed",
    "field_moved",
    "response_structure_changed",
    "operation_removed",
    "enum_values_removed",
    "nested_field_removed",
    "nested_field_type_changed",
    "array_item_type_changed",
}


@dataclass
class ClassifiedChange:
    is_breaking: bool
    severity: str           # critical, high, medium, low
    summary: str
    changed_routes: list[str]
    changed_fields: list[dict]
    diffs: list[ContractDiff]


def classify_changes(diffs: list[ContractDiff]) -> ClassifiedChange:
    """Classify a set of diffs into a single classified change."""
    if not diffs:
        return ClassifiedChange(
            is_breaking=False,
            severity="low",
            summary="No changes detected",
            changed_routes=[],
            changed_fields=[],
            diffs=[],
        )

    breaking_diffs = [d for d in diffs if d.diff_type in BREAKING_DIFF_TYPES]
    is_breaking = len(breaking_diffs) > 0

    # Determine severity
    has_required_field_add = any(
        d.diff_type in ("field_added_required", "field_optional_to_required") for d in diffs
    )
    has_structure_change = any(d.diff_type == "response_structure_changed" for d in diffs)
    has_field_removed = any(
        d.diff_type in ("field_removed", "nested_field_removed") for d in diffs
    )
    has_type_change = any(
        d.diff_type in ("field_type_changed", "nested_field_type_changed", "array_item_type_changed")
        for d in diffs
    )
    has_enum_narrowing = any(d.diff_type == "enum_values_removed" for d in diffs)

    if has_required_field_add or has_structure_change:
        severity = "critical"
    elif has_field_removed or has_enum_narrowing:
        severity = "high"
    elif has_type_change:
        severity = "medium"
    else:
        severity = "low"

    # Build summary
    parts = []
    if has_required_field_add:
        fields = [d.field for d in diffs if d.diff_type in ("field_added_required", "field_optional_to_required")]
        parts.append(f"New required field(s): {', '.join(fields)}")
    if has_field_removed:
        fields = [d.field for d in diffs if d.diff_type in ("field_removed", "nested_field_removed")]
        parts.append(f"Removed field(s): {', '.join(fields)}")
    if has_structure_change:
        fields = [d.field for d in diffs if d.diff_type == "response_structure_changed"]
        parts.append(f"Response structure changed: {', '.join(fields)}")
    if has_type_change:
        fields = [d.field for d in diffs if d.diff_type in ("field_type_changed", "nested_field_type_changed", "array_item_type_changed")]
        parts.append(f"Type changed: {', '.join(fields)}")
    if has_enum_narrowing:
        fields = [d.field for d in diffs if d.diff_type == "enum_values_removed"]
        parts.append(f"Enum values removed: {', '.join(fields)}")

    summary = "; ".join(parts) if parts else "Non-breaking changes detected"

    # Extract unique changed routes
    changed_routes = sorted(set(f"{d.method.upper()} {d.path}" for d in diffs))

    # Build changed fields list
    changed_fields = [
        {
            "path": d.path,
            "method": d.method,
            "field": d.field,
            "diff_type": d.diff_type,
            "old_value": str(d.old_value) if d.old_value is not None else None,
            "new_value": str(d.new_value) if d.new_value is not None else None,
        }
        for d in diffs
    ]

    return ClassifiedChange(
        is_breaking=is_breaking,
        severity=severity,
        summary=summary,
        changed_routes=changed_routes,
        changed_fields=changed_fields,
        diffs=diffs,
    )
