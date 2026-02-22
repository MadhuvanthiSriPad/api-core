"""Safety guardrails for propagation jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Guardrails:
    max_parallel: int = 3
    protected_paths: list[str] = field(default_factory=lambda: [
        "infra/",
        ".github/workflows/",
        "terraform/",
        "k8s/",
    ])
    ci_required: bool = True
    auto_merge: bool = False

    def print_config(self):
        print("=" * 60)
        print("PROPAGATION GUARDRAILS")
        print("=" * 60)
        print(f"  MAX_PARALLEL   = {self.max_parallel}")
        print(f"  PROTECTED_PATHS= {self.protected_paths}")
        print(f"  AUTO_MERGE     = {self.auto_merge}")
        print(f"  CI_REQUIRED    = {self.ci_required}")
        print("=" * 60)

    def validate_paths(self, client_paths: list[str]) -> list[str]:
        """Check client_paths against protected_paths. Returns list of violations."""
        violations = []
        for path in client_paths:
            for protected in self.protected_paths:
                if path.startswith(protected):
                    violations.append(f"{path} is under protected path {protected}")
        return violations

    def check_can_merge(self, ci_passed: bool) -> tuple[bool, str]:
        """Check whether a PR can be merged given guardrail constraints.

        Returns (allowed, reason).
        """
        if not self.auto_merge:
            return False, "auto_merge is disabled — PR requires human review"
        if self.ci_required and not ci_passed:
            return False, "ci_required is enabled but CI has not passed"
        return True, "merge allowed"


class GuardrailViolation(Exception):
    """Raised when a guardrail check fails."""
    pass


def load_guardrails() -> Guardrails:
    """Load guardrails from environment or defaults."""
    import os
    return Guardrails(
        max_parallel=int(os.getenv("PROPAGATE_MAX_PARALLEL", "3")),
        auto_merge=os.getenv("PROPAGATE_AUTO_MERGE", "false").lower() == "true",
        ci_required=os.getenv("PROPAGATE_CI_REQUIRED", "true").lower() == "true",
    )
