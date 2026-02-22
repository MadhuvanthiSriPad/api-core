"""Service dependency graph builder and topological sorter."""

from __future__ import annotations
from typing import Dict, List, Set
from dataclasses import dataclass


@dataclass
class ServiceNode:
    """Represents a service in the dependency graph."""
    name: str
    depends_on: List[str]  # List of services this service depends on


class DependencyGraph:
    """Builds and analyzes service dependency graphs."""

    def __init__(self):
        self.nodes: Dict[str, ServiceNode] = {}

    def add_service(self, name: str, depends_on: List[str] = None):
        """Add a service to the graph."""
        self.nodes[name] = ServiceNode(
            name=name,
            depends_on=depends_on or []
        )

    def topological_sort(self) -> List[List[str]]:
        """
        Return services grouped by dependency waves.

        Returns:
            List of waves, where each wave is a list of services
            that can be processed in parallel.

        Example:
            [
                ["api-core"],  # Wave 0: No dependencies
                ["billing-service", "dashboard-service"],  # Wave 1: Depend on api-core
                ["invoice-service"]  # Wave 2: Depends on billing-service
            ]
        """
        # Count incoming edges for each node
        in_degree = {name: 0 for name in self.nodes}
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep in in_degree:
                    in_degree[dep] += 1

        waves = []
        processed = set()

        while len(processed) < len(self.nodes):
            # Find all nodes with no remaining dependencies
            current_wave = [
                name for name, degree in in_degree.items()
                if degree == 0 and name not in processed
            ]

            if not current_wave:
                # Circular dependency detected
                remaining = set(self.nodes.keys()) - processed
                raise ValueError(f"Circular dependency detected in: {remaining}")

            waves.append(sorted(current_wave))
            processed.update(current_wave)

            # Reduce in-degree for nodes depending on current wave
            for service in current_wave:
                if service in self.nodes:
                    for dep in self.nodes[service].depends_on:
                        if dep in in_degree:
                            in_degree[dep] -= 1

        return waves

    def get_affected_services(
        self,
        changed_services: List[str]
    ) -> List[str]:
        """
        Find all services affected by changes to given services.

        Args:
            changed_services: Services that changed

        Returns:
            All services that depend (directly or indirectly) on changed services
        """
        affected = set()
        queue = list(changed_services)

        while queue:
            service = queue.pop(0)

            # Find services that depend on this service
            for node in self.nodes.values():
                if service in node.depends_on and node.name not in affected:
                    affected.add(node.name)
                    queue.append(node.name)

        return sorted(affected)


def build_dependency_graph_from_service_map(service_map: dict) -> DependencyGraph:
    """
    Build dependency graph from service_map.yaml structure.

    Args:
        service_map: Parsed service_map.yaml

    Returns:
        DependencyGraph instance
    """
    graph = DependencyGraph()

    # Add api-core as root (no dependencies)
    graph.add_service("api-core", depends_on=[])

    # Add other services (all depend on api-core for now)
    for service_name, service_info in service_map.items():
        dependencies = service_info.get("depends_on", ["api-core"])
        graph.add_service(service_name, depends_on=dependencies)

    return graph
