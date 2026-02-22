"""Tests for the guardrails module."""

import os
import pytest

from propagate.guardrails import Guardrails, load_guardrails


class TestValidatePaths:
    def test_no_violations(self):
        g = Guardrails()
        assert g.validate_paths(["src/client.py", "tests/test_client.py"]) == []

    def test_infra_violation(self):
        g = Guardrails()
        violations = g.validate_paths(["infra/main.tf", "src/app.py"])
        assert len(violations) == 1
        assert "infra/" in violations[0]

    def test_workflow_violation(self):
        g = Guardrails()
        violations = g.validate_paths([".github/workflows/ci.yaml"])
        assert len(violations) == 1

    def test_terraform_violation(self):
        g = Guardrails()
        violations = g.validate_paths(["terraform/modules/rds.tf"])
        assert len(violations) == 1

    def test_k8s_violation(self):
        g = Guardrails()
        violations = g.validate_paths(["k8s/deployment.yaml"])
        assert len(violations) == 1

    def test_multiple_violations(self):
        g = Guardrails()
        violations = g.validate_paths(["infra/x", "terraform/y", ".github/workflows/z"])
        assert len(violations) == 3

    def test_empty_paths(self):
        g = Guardrails()
        assert g.validate_paths([]) == []

    def test_custom_protected_paths(self):
        g = Guardrails(protected_paths=["secret/"])
        violations = g.validate_paths(["secret/keys.json"])
        assert len(violations) == 1
        assert g.validate_paths(["infra/main.tf"]) == []


class TestCheckCanMerge:
    def test_auto_merge_disabled(self):
        g = Guardrails(auto_merge=False)
        allowed, reason = g.check_can_merge(ci_passed=True)
        assert allowed is False
        assert "auto_merge is disabled" in reason

    def test_ci_required_not_passed(self):
        g = Guardrails(auto_merge=True, ci_required=True)
        allowed, reason = g.check_can_merge(ci_passed=False)
        assert allowed is False
        assert "CI has not passed" in reason

    def test_merge_allowed(self):
        g = Guardrails(auto_merge=True, ci_required=True)
        allowed, reason = g.check_can_merge(ci_passed=True)
        assert allowed is True
        assert "merge allowed" in reason

    def test_merge_allowed_ci_not_required(self):
        g = Guardrails(auto_merge=True, ci_required=False)
        allowed, reason = g.check_can_merge(ci_passed=False)
        assert allowed is True


class TestLoadGuardrails:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("PROPAGATE_MAX_PARALLEL", raising=False)
        monkeypatch.delenv("PROPAGATE_AUTO_MERGE", raising=False)
        monkeypatch.delenv("PROPAGATE_CI_REQUIRED", raising=False)
        g = load_guardrails()
        assert g.max_parallel == 3
        assert g.auto_merge is False
        assert g.ci_required is True

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PROPAGATE_MAX_PARALLEL", "5")
        monkeypatch.setenv("PROPAGATE_AUTO_MERGE", "true")
        monkeypatch.setenv("PROPAGATE_CI_REQUIRED", "false")
        g = load_guardrails()
        assert g.max_parallel == 5
        assert g.auto_merge is True
        assert g.ci_required is False


class TestEdgeCases:
    def test_nested_protected_path(self):
        """Deeply nested files under a protected prefix should be caught."""
        g = Guardrails()
        violations = g.validate_paths(["infra/nested/deep/file.tf"])
        assert len(violations) == 1
        assert "infra/" in violations[0]

    def test_path_prefix_but_not_under_protected(self):
        """A path that starts with the same chars but isn't under the protected dir."""
        g = Guardrails()
        # "infrastructure/" is not under "infra/" (different directory)
        violations = g.validate_paths(["infrastructure/main.tf"])
        assert len(violations) == 0

    def test_guardrail_violation_exception(self):
        """GuardrailViolation is a proper exception class."""
        from propagate.guardrails import GuardrailViolation
        exc = GuardrailViolation("test violation")
        assert str(exc) == "test violation"
        assert isinstance(exc, Exception)
