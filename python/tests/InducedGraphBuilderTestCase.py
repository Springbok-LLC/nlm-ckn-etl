from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import networkx as nx

from InducedGraphBuilder import (
    descendants_at_depth,
    add_ontology_hierarchy_paths,
    _is_self_referential_edge,
)


def build_test_graph():
    """Build a small test graph:

    CS/1 --[CS-CL]--> CL/1 --[CL-GO]--> GO/1
    CL/1 --[CL-CL, SUB_CLASS_OF]--> CL/2  (self-referential)
    GO/1 --[GO-GO, SUB_CLASS_OF]--> GO/2  (self-referential)
    CHEMBL/1 --[CHEMBL-CL]--> CL/1  (incoming to CL/1 from nothing else)
    CHEMBL/1 --[CHEMBL-PR]--> PR/1
    CS/1 --[CS-CHEBI]--> CHEBI/1 --[CHEBI-PR]--> PR/2  (CHEBI blocked)
    CS/1 --[CS-NCT]--> NCT/1  (NCT blocked)
    CS/1 --[CS-UBERON]--> UBERON/1
    UBERON/1 --[UBERON-UBERON, PART_OF]--> UBERON/2  (self-referential)
    """
    G = nx.MultiDiGraph()

    nodes = [
        "CS/1",
        "CL/1",
        "CL/2",
        "GO/1",
        "GO/2",
        "CHEMBL/1",
        "PR/1",
        "PR/2",
        "CHEBI/1",
        "NCT/1",
        "UBERON/1",
        "UBERON/2",
    ]
    for n in nodes:
        G.add_node(n)

    G.add_edge("CS/1", "CL/1", key="e1", _id="CS-CL/e1", _key="e1")
    G.add_edge("CL/1", "GO/1", key="e2", _id="CL-GO/e2", _key="e2")
    G.add_edge(
        "CL/1", "CL/2", key="e3", _id="CL-CL/e3", _key="e3", Label="SUB_CLASS_OF"
    )
    G.add_edge(
        "GO/1", "GO/2", key="e4", _id="GO-GO/e4", _key="e4", Label="SUB_CLASS_OF"
    )
    G.add_edge("CHEMBL/1", "CL/1", key="e5", _id="CHEMBL-CL/e5", _key="e5")
    G.add_edge("CHEMBL/1", "PR/1", key="e6", _id="CHEMBL-PR/e6", _key="e6")
    G.add_edge("CS/1", "CHEBI/1", key="e7", _id="CS-CHEBI/e7", _key="e7")
    G.add_edge("CHEBI/1", "PR/2", key="e8", _id="CHEBI-PR/e8", _key="e8")
    G.add_edge("CS/1", "NCT/1", key="e9", _id="CS-NCT/e9", _key="e9")
    G.add_edge("CS/1", "UBERON/1", key="e10", _id="CS-UBERON/e10", _key="e10")
    G.add_edge(
        "UBERON/1",
        "UBERON/2",
        key="e11",
        _id="UBERON-UBERON/e11",
        _key="e11",
        Label="PART_OF",
    )

    return G


def find_induced(G, source_collection="CS", max_depth=10):
    """Run BFS from source_collection vertices and return the induced subgraph."""
    source_vertices = {v for v in G.nodes if v.startswith(f"{source_collection}/")}
    reachable = set()
    for source in source_vertices:
        reachable.update(descendants_at_depth(G, source, max_depth))
    reachable.update(source_vertices)
    return G.subgraph(reachable).copy()


def edge_keys(induced):
    """Return set of edge keys in the induced subgraph."""
    return {key for _, _, key in induced.edges(keys=True)}


class DescendantsAtDepthTestCase(unittest.TestCase):
    """Tests for the BFS traversal function."""

    def setUp(self):
        self.G = build_test_graph()

    def test_bidirectional_reachability(self):
        """CHEMBL/1 is reachable via incoming edge from CL/1."""
        induced = find_induced(self.G)
        self.assertIn("CHEMBL/1", induced.nodes)

    def test_bidirectional_preserves_edge_direction(self):
        """CHEMBL->PR edge should be present with correct direction."""
        induced = find_induced(self.G)
        self.assertIn("e6", edge_keys(induced))
        self.assertTrue(induced.has_edge("CHEMBL/1", "PR/1"))

    def test_ignored_collections_chebi_excluded(self):
        """CHEBI vertices should be excluded."""
        induced = find_induced(self.G)
        self.assertNotIn("CHEBI/1", induced.nodes)

    def test_ignored_collections_nct_excluded(self):
        """NCT vertices should be excluded."""
        induced = find_induced(self.G)
        self.assertNotIn("NCT/1", induced.nodes)

    def test_ignored_collections_blocks_traversal_beyond(self):
        """PR/2 is only reachable through CHEBI/1, which is blocked."""
        induced = find_induced(self.G)
        self.assertNotIn("PR/2", induced.nodes)

    def test_self_referential_edges_skipped_in_bfs(self):
        """Vertices reachable only via self-referential edges are not found by BFS."""
        induced = find_induced(self.G)
        self.assertNotIn("CL/2", induced.nodes)
        self.assertNotIn("GO/2", induced.nodes)
        self.assertNotIn("UBERON/2", induced.nodes)

    def test_depth_limit_respected(self):
        """With depth 1, only direct neighbors via cross-collection edges are found."""
        induced = find_induced(self.G, max_depth=1)
        self.assertIn("CS/1", induced.nodes)
        self.assertIn("CL/1", induced.nodes)
        self.assertIn("UBERON/1", induced.nodes)
        self.assertNotIn("GO/1", induced.nodes)

    def test_depth_boundary_vertex_included(self):
        """Vertex at exactly max_depth is included."""
        induced = find_induced(self.G, max_depth=2)
        self.assertIn("GO/1", induced.nodes)

    def test_all_edges_between_reachable_vertices_included(self):
        """Cross-collection edges between reachable vertices should all be present."""
        induced = find_induced(self.G)
        keys = edge_keys(induced)
        for key in ("e1", "e2", "e5", "e6", "e10"):
            self.assertIn(key, keys, f"Edge {key} should be in induced subgraph")

    def test_edges_to_ignored_collections_excluded(self):
        """Edges involving CHEBI or NCT should not appear."""
        induced = find_induced(self.G)
        keys = edge_keys(induced)
        for key in ("e7", "e8", "e9"):
            self.assertNotIn(key, keys, f"Edge {key} should be excluded")


class IsSelfReferentialEdgeTestCase(unittest.TestCase):
    """Tests for the self-referential edge helper."""

    def test_self_referential(self):
        self.assertTrue(_is_self_referential_edge("CL-CL/e1"))
        self.assertTrue(_is_self_referential_edge("GO-GO/e2"))

    def test_cross_collection(self):
        self.assertFalse(_is_self_referential_edge("CL-GO/e1"))
        self.assertFalse(_is_self_referential_edge("CS-CL/e2"))

    def test_empty(self):
        self.assertFalse(_is_self_referential_edge(""))


class AddOntologyHierarchyPathsTestCase(unittest.TestCase):
    """Tests for hierarchy enrichment."""

    def setUp(self):
        self.G = build_test_graph()
        self.induced = find_induced(self.G)

    def test_walk_adds_go_ancestors(self):
        """GO/2 should be added via walk from GO/1 following SUB_CLASS_OF."""
        add_ontology_hierarchy_paths(self.G, self.induced)
        self.assertIn("GO/2", self.induced.nodes)
        self.assertIn("e4", edge_keys(self.induced))

    def test_walk_adds_uberon_ancestors(self):
        """UBERON/2 should be added via walk from UBERON/1 following PART_OF."""
        add_ontology_hierarchy_paths(self.G, self.induced)
        self.assertIn("UBERON/2", self.induced.nodes)
        self.assertIn("e11", edge_keys(self.induced))

    def test_all_strategy_includes_entire_collection(self):
        """CL uses 'all' strategy — CL/2 and the CL-CL edge should be included."""
        add_ontology_hierarchy_paths(self.G, self.induced)
        self.assertIn("CL/2", self.induced.nodes)
        self.assertIn("e3", edge_keys(self.induced))

    def test_no_duplicate_vertices(self):
        """Vertices already in the induced subgraph should not be duplicated."""
        add_ontology_hierarchy_paths(self.G, self.induced)
        cl1_count = sum(1 for n in self.induced.nodes if n == "CL/1")
        self.assertEqual(1, cl1_count)


if __name__ == "__main__":
    unittest.main()
