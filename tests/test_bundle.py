"""Tests for the bundle builder module."""

import pytest

from propagate.bundle import build_fix_bundles, RepoFixBundle
from propagate.classifier import ClassifiedChange
from propagate.differ import ContractDiff
from propagate.impact import ImpactRecord
from propagate.service_map import ServiceInfo


def _classified(summary="Test change", severity="critical", is_breaking=True):
    return ClassifiedChange(
        is_breaking=is_breaking,
        severity=severity,
        summary=summary,
        changed_routes=["POST /api/v1/sessions"],
        changed_fields=[{
            "path": "/api/v1/sessions", "method": "post",
            "field": "request.body.priority", "diff_type": "field_added_required",
            "old_value": None, "new_value": "string",
        }],
        diffs=[ContractDiff(
            path="/api/v1/sessions", method="post",
            field="request.body.priority", old_value=None,
            new_value="string", diff_type="field_added_required",
        )],
    )


def _impact(caller="billing-service", route="/api/v1/sessions", method="POST", calls=42):
    return ImpactRecord(
        caller_service=caller,
        route_template=route,
        method=method,
        calls_last_7d=calls,
    )


def _service_map():
    return {
        "billing-service": ServiceInfo(
            repo="org/billing-service",
            client_paths=["src/api_client.py"],
            test_paths=["tests/test_api.py"],
            depends_on=["api-core"],
        ),
        "dashboard-service": ServiceInfo(
            repo="org/dashboard-service",
            client_paths=["src/core_client.ts"],
            test_paths=["tests/api.test.ts"],
            frontend_paths=["src/components/"],
            depends_on=["api-core"],
        ),
    }


class TestBuildFixBundles:
    def test_builds_bundle_per_service(self):
        impacts = [
            _impact(caller="billing-service"),
            _impact(caller="dashboard-service"),
        ]
        bundles = build_fix_bundles(_classified(), impacts, _service_map())
        assert len(bundles) == 2
        services = {b.target_service for b in bundles}
        assert services == {"billing-service", "dashboard-service"}

    def test_bundle_fields(self):
        bundles = build_fix_bundles(
            _classified(), [_impact()], _service_map()
        )
        b = bundles[0]
        assert b.target_repo == "org/billing-service"
        assert b.target_service == "billing-service"
        assert b.call_count_7d == 42
        assert len(b.affected_routes) == 1
        assert "POST" in b.affected_routes[0]
        assert b.client_paths == ["src/api_client.py"]
        assert b.test_paths == ["tests/test_api.py"]

    def test_prompt_contains_key_info(self):
        bundles = build_fix_bundles(
            _classified(summary="New required field: priority"),
            [_impact()], _service_map(),
        )
        prompt = bundles[0].prompt
        assert "billing-service" in prompt
        assert "priority" in prompt
        assert "breaking" in prompt.lower() or "BREAKING" in prompt or "Breaking" in prompt

    def test_hash_stability(self):
        """Same inputs should produce the same bundle hash."""
        b1 = build_fix_bundles(_classified(), [_impact()], _service_map())
        b2 = build_fix_bundles(_classified(), [_impact()], _service_map())
        assert b1[0].bundle_hash == b2[0].bundle_hash

    def test_hash_changes_with_different_summary(self):
        b1 = build_fix_bundles(_classified(summary="change A"), [_impact()], _service_map())
        b2 = build_fix_bundles(_classified(summary="change B"), [_impact()], _service_map())
        assert b1[0].bundle_hash != b2[0].bundle_hash

    def test_skips_unknown_service(self):
        impacts = [_impact(caller="unknown-service")]
        bundles = build_fix_bundles(_classified(), impacts, _service_map())
        assert len(bundles) == 0

    def test_aggregates_calls_per_service(self):
        impacts = [
            _impact(caller="billing-service", route="/a", calls=10),
            _impact(caller="billing-service", route="/b", calls=20),
        ]
        bundles = build_fix_bundles(_classified(), impacts, _service_map())
        assert len(bundles) == 1
        assert bundles[0].call_count_7d == 30

    def test_frontend_paths_in_prompt(self):
        impacts = [_impact(caller="dashboard-service")]
        bundles = build_fix_bundles(_classified(), impacts, _service_map())
        prompt = bundles[0].prompt
        assert "Frontend" in prompt
        assert "src/components/" in prompt


class TestRepoFixBundleHash:
    def test_auto_generated(self):
        b = RepoFixBundle(
            target_repo="org/test",
            target_service="test",
            change_summary="test",
            breaking_changes=[],
            affected_routes=["/a"],
            call_count_7d=1,
            client_paths=[],
            test_paths=[],
            frontend_paths=[],
            prompt="test prompt",
        )
        assert b.bundle_hash != ""
        assert len(b.bundle_hash) == 16

    def test_explicit_hash_preserved(self):
        b = RepoFixBundle(
            target_repo="org/test",
            target_service="test",
            change_summary="test",
            breaking_changes=[],
            affected_routes=["/a"],
            call_count_7d=1,
            client_paths=[],
            test_paths=[],
            frontend_paths=[],
            prompt="test prompt",
            bundle_hash="custom_hash_12345",
        )
        assert b.bundle_hash == "custom_hash_12345"
