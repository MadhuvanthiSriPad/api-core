"""Tests for the contract change classifier module."""

import pytest

from propagate.differ import ContractDiff
from propagate.classifier import classify_changes, BREAKING_DIFF_TYPES


def _diff(diff_type, field="test.field", path="/test", method="post"):
    return ContractDiff(
        path=path, method=method, field=field,
        old_value="old", new_value="new", diff_type=diff_type,
    )


class TestClassifyChanges:
    def test_empty_diffs(self):
        result = classify_changes([])
        assert result.is_breaking is False
        assert result.severity == "low"
        assert result.changed_routes == []
        assert result.changed_fields == []

    def test_breaking_detection(self):
        for dt in BREAKING_DIFF_TYPES:
            result = classify_changes([_diff(dt)])
            assert result.is_breaking is True, f"{dt} should be breaking"

    def test_non_breaking(self):
        result = classify_changes([_diff("operation_added")])
        assert result.is_breaking is False

    def test_severity_critical_required_field(self):
        result = classify_changes([_diff("field_added_required")])
        assert result.severity == "critical"

    def test_severity_critical_optional_to_required(self):
        result = classify_changes([_diff("field_optional_to_required")])
        assert result.severity == "critical"

    def test_severity_critical_structure_change(self):
        result = classify_changes([_diff("response_structure_changed")])
        assert result.severity == "critical"

    def test_severity_high_field_removed(self):
        result = classify_changes([_diff("field_removed")])
        assert result.severity == "high"

    def test_severity_high_enum_narrowing(self):
        result = classify_changes([_diff("enum_values_removed")])
        assert result.severity == "high"

    def test_severity_medium_type_change(self):
        result = classify_changes([_diff("field_type_changed")])
        assert result.severity == "medium"

    def test_severity_low_non_breaking(self):
        result = classify_changes([_diff("operation_added")])
        assert result.severity == "low"

    def test_changed_routes_format(self):
        diffs = [
            _diff("field_added_required", path="/a", method="post"),
            _diff("field_removed", path="/b", method="get"),
            _diff("field_type_changed", path="/a", method="post"),
        ]
        result = classify_changes(diffs)
        assert "GET /b" in result.changed_routes
        assert "POST /a" in result.changed_routes
        # Should be deduplicated
        assert len(result.changed_routes) == 2

    def test_summary_includes_required_fields(self):
        result = classify_changes([
            _diff("field_added_required", field="request.body.priority"),
        ])
        assert "priority" in result.summary

    def test_summary_includes_removed_fields(self):
        result = classify_changes([
            _diff("field_removed", field="request.body.old_field"),
        ])
        assert "old_field" in result.summary

    def test_changed_fields_structure(self):
        result = classify_changes([_diff("field_type_changed", path="/x", method="put")])
        assert len(result.changed_fields) == 1
        cf = result.changed_fields[0]
        assert cf["path"] == "/x"
        assert cf["method"] == "put"
        assert cf["diff_type"] == "field_type_changed"

    def test_multiple_diff_types_highest_severity_wins(self):
        """When multiple diff types are present, the highest severity should win."""
        diffs = [
            _diff("field_type_changed"),      # medium
            _diff("field_added_required"),     # critical
        ]
        result = classify_changes(diffs)
        assert result.severity == "critical"

    def test_diffs_preserved(self):
        d = _diff("field_removed")
        result = classify_changes([d])
        assert result.diffs == [d]

    def test_nested_field_removed_is_breaking(self):
        result = classify_changes([_diff("nested_field_removed")])
        assert result.is_breaking is True

    def test_priority_ordering_multiple_severities(self):
        """When multiple severity levels are present, highest wins."""
        diffs = [
            _diff("operation_added"),          # low (non-breaking)
            _diff("field_type_changed"),        # medium
            _diff("field_removed"),             # high
            _diff("field_added_required"),       # critical
        ]
        result = classify_changes(diffs)
        assert result.severity == "critical"
        assert result.is_breaking is True

    def test_summary_for_type_change(self):
        result = classify_changes([_diff("field_type_changed", field="request.body.count")])
        assert "count" in result.summary

    def test_summary_for_enum_narrowing(self):
        result = classify_changes([_diff("enum_values_removed", field="request.body.status")])
        assert "status" in result.summary

    def test_summary_for_structure_change(self):
        result = classify_changes([_diff("response_structure_changed", field="response.200.usage")])
        assert "usage" in result.summary
