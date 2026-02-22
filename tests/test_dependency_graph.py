"""Tests for the dependency graph module."""

import pytest

from propagate.dependency_graph import DependencyGraph


class TestTopologicalSort:
    def test_single_node(self):
        g = DependencyGraph()
        g.add_service("api-core", depends_on=[])
        waves = g.topological_sort()
        assert waves == [["api-core"]]

    def test_linear_chain(self):
        g = DependencyGraph()
        g.add_service("a", depends_on=[])
        g.add_service("b", depends_on=["a"])
        g.add_service("c", depends_on=["b"])
        waves = g.topological_sort()
        assert waves == [["a"], ["b"], ["c"]]

    def test_parallel_deps(self):
        g = DependencyGraph()
        g.add_service("root", depends_on=[])
        g.add_service("svc-a", depends_on=["root"])
        g.add_service("svc-b", depends_on=["root"])
        waves = g.topological_sort()
        assert waves[0] == ["root"]
        assert sorted(waves[1]) == ["svc-a", "svc-b"]

    def test_diamond_dependency(self):
        """A -> B, A -> C, B -> D, C -> D (diamond shape)."""
        g = DependencyGraph()
        g.add_service("a", depends_on=[])
        g.add_service("b", depends_on=["a"])
        g.add_service("c", depends_on=["a"])
        g.add_service("d", depends_on=["b", "c"])
        waves = g.topological_sort()
        assert waves[0] == ["a"]
        assert sorted(waves[1]) == ["b", "c"]
        assert waves[2] == ["d"]

    def test_circular_dependency_raises(self):
        g = DependencyGraph()
        g.add_service("a", depends_on=["b"])
        g.add_service("b", depends_on=["a"])
        with pytest.raises(ValueError, match="Circular dependency"):
            g.topological_sort()

    def test_empty_graph(self):
        g = DependencyGraph()
        assert g.topological_sort() == []

    def test_unresolved_dep_ignored(self):
        """Dependencies on services not in the graph should not block sorting."""
        g = DependencyGraph()
        g.add_service("a", depends_on=["nonexistent"])
        # 'nonexistent' is not in the graph, so in_degree for 'a' stays 0
        waves = g.topological_sort()
        assert waves == [["a"]]


class TestGetAffectedServices:
    def test_direct_dependency(self):
        g = DependencyGraph()
        g.add_service("api-core", depends_on=[])
        g.add_service("billing", depends_on=["api-core"])
        affected = g.get_affected_services(["api-core"])
        assert "billing" in affected

    def test_transitive_dependency(self):
        g = DependencyGraph()
        g.add_service("a", depends_on=[])
        g.add_service("b", depends_on=["a"])
        g.add_service("c", depends_on=["b"])
        affected = g.get_affected_services(["a"])
        assert "b" in affected
        assert "c" in affected

    def test_no_affected(self):
        g = DependencyGraph()
        g.add_service("a", depends_on=[])
        g.add_service("b", depends_on=[])
        assert g.get_affected_services(["a"]) == []

    def test_changed_service_not_in_result(self):
        g = DependencyGraph()
        g.add_service("a", depends_on=[])
        g.add_service("b", depends_on=["a"])
        affected = g.get_affected_services(["a"])
        assert "a" not in affected


class TestEdgeCases:
    def test_self_cycle_raises(self):
        """A service depending on itself should be detected as circular."""
        g = DependencyGraph()
        g.add_service("a", depends_on=["a"])
        with pytest.raises(ValueError, match="Circular dependency"):
            g.topological_sort()

    def test_larger_graph(self):
        """5+ node graph with mixed dependency chains."""
        g = DependencyGraph()
        g.add_service("root", depends_on=[])
        g.add_service("a", depends_on=["root"])
        g.add_service("b", depends_on=["root"])
        g.add_service("c", depends_on=["a"])
        g.add_service("d", depends_on=["a", "b"])
        g.add_service("e", depends_on=["c", "d"])
        waves = g.topological_sort()
        assert waves[0] == ["root"]
        assert sorted(waves[1]) == ["a", "b"]
        # c depends on a only; d depends on a and b
        assert "c" in waves[2]
        assert "d" in waves[2]
        assert waves[3] == ["e"]

    def test_wave_ordering_independent_chains(self):
        """Two independent chains should be processed in parallel."""
        g = DependencyGraph()
        g.add_service("x1", depends_on=[])
        g.add_service("x2", depends_on=["x1"])
        g.add_service("y1", depends_on=[])
        g.add_service("y2", depends_on=["y1"])
        waves = g.topological_sort()
        assert sorted(waves[0]) == ["x1", "y1"]
        assert sorted(waves[1]) == ["x2", "y2"]

    def test_build_from_service_map(self):
        from propagate.dependency_graph import build_dependency_graph_from_service_map
        from propagate.service_map import ServiceInfo

        svc_map = {
            "billing-service": ServiceInfo(
                repo="org/billing",
                client_paths=["src/client.py"],
                depends_on=["api-core"],
            ),
            "dashboard-service": ServiceInfo(
                repo="org/dashboard",
                client_paths=["src/main.py"],
                depends_on=["api-core"],
            ),
        }
        graph = build_dependency_graph_from_service_map(svc_map)
        waves = graph.topological_sort()
        assert waves[0] == ["api-core"]
        assert sorted(waves[1]) == ["billing-service", "dashboard-service"]
