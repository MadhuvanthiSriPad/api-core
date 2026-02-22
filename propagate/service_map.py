"""Load the service-to-repo mapping from service_map.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServiceInfo:
    repo: str
    language: str = "python"
    client_paths: list[str] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=list)
    frontend_paths: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


def load_service_map(path: str | None = None) -> dict[str, ServiceInfo]:
    """Load service_map.yaml and return a dict mapping service name to ServiceInfo."""
    if path is None:
        path = str(Path(__file__).resolve().parent.parent / "service_map.yaml")

    with open(path) as f:
        data = yaml.safe_load(f)

    result: dict[str, ServiceInfo] = {}
    for svc_name, svc_data in data.get("services", {}).items():
        result[svc_name] = ServiceInfo(
            repo=svc_data["repo"],
            language=svc_data.get("language", "python"),
            client_paths=svc_data.get("client_paths", []),
            test_paths=svc_data.get("test_paths", []),
            frontend_paths=svc_data.get("frontend_paths", []),
            depends_on=svc_data.get("depends_on", []),
        )
    return result
