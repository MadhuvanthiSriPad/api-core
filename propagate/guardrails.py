"""Safety guardrails for propagation jobs."""

from __future__ import annotations

from dataclasses import dataclass, field


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


def load_guardrails() -> Guardrails:
    """Load guardrails from environment or defaults."""
    import os
    return Guardrails(
        max_parallel=int(os.getenv("PROPAGATE_MAX_PARALLEL", "3")),
        auto_merge=os.getenv("PROPAGATE_AUTO_MERGE", "false").lower() == "true",
        ci_required=os.getenv("PROPAGATE_CI_REQUIRED", "true").lower() == "true",
    )
