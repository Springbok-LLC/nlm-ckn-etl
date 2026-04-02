package gov.nih.nlm;

import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;
import org.junit.jupiter.api.Test;

import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.*;

class InducedSubgraphFinderTest {

    private final InducedSubgraphFinder finder = new InducedSubgraphFinder();

    // -- Vertex helpers --

    private static ArangoVertex vertex(String collection, String key) {
        return new ArangoVertex(collection + "/" + key, collection, key, Map.of());
    }

    // -- Edge helpers --

    private static ArangoEdge edge(String key, ArangoVertex from, ArangoVertex to, String edgeCollection) {
        return new ArangoEdge(key, from.id(), to.id(), edgeCollection, Map.of());
    }

    private static ArangoEdge edge(String key, ArangoVertex from, ArangoVertex to, String edgeCollection,
                                   Map<String, Object> props) {
        return new ArangoEdge(key, from.id(), to.id(), edgeCollection, props);
    }

    // -- Graph fixture --

    /**
     * Build a small test graph:
     *
     *   CS/1 --[CS-CL]--> CL/1 --[CL-GO]--> GO/1
     *   CL/1 --[CL-CL, SUB_CLASS_OF]--> CL/2  (self-referential)
     *   GO/1 --[GO-GO, SUB_CLASS_OF]--> GO/2  (self-referential)
     *   CHEMBL/1 --[CHEMBL-CL]--> CL/1  (incoming to CL/1 from nothing else)
     *   CHEMBL/1 --[CHEMBL-PR]--> PR/1
     *   CS/1 --[CS-CHEBI]--> CHEBI/1 --[CHEBI-PR]--> PR/2  (CHEBI blocked)
     *   CS/1 --[CS-NCT]--> NCT/1  (NCT blocked)
     *   CS/1 --[CS-UBERON]--> UBERON/1
     *   UBERON/1 --[UBERON-UBERON, PART_OF]--> UBERON/2  (self-referential)
     */
    private record TestGraph(
            DirectedPseudograph<ArangoVertex, ArangoEdge> graph,
            ArangoVertex cs1, ArangoVertex cl1, ArangoVertex cl2,
            ArangoVertex go1, ArangoVertex go2,
            ArangoVertex chembl1, ArangoVertex pr1, ArangoVertex pr2,
            ArangoVertex chebi1, ArangoVertex nct1,
            ArangoVertex uberon1, ArangoVertex uberon2) {}

    private TestGraph buildTestGraph() {
        DirectedPseudograph<ArangoVertex, ArangoEdge> g = new DirectedPseudograph<>(ArangoEdge.class);

        ArangoVertex cs1 = vertex("CS", "1");
        ArangoVertex cl1 = vertex("CL", "1");
        ArangoVertex cl2 = vertex("CL", "2");
        ArangoVertex go1 = vertex("GO", "1");
        ArangoVertex go2 = vertex("GO", "2");
        ArangoVertex chembl1 = vertex("CHEMBL", "1");
        ArangoVertex pr1 = vertex("PR", "1");
        ArangoVertex pr2 = vertex("PR", "2");
        ArangoVertex chebi1 = vertex("CHEBI", "1");
        ArangoVertex nct1 = vertex("NCT", "1");
        ArangoVertex uberon1 = vertex("UBERON", "1");
        ArangoVertex uberon2 = vertex("UBERON", "2");

        for (ArangoVertex v : new ArangoVertex[]{cs1, cl1, cl2, go1, go2, chembl1, pr1, pr2,
                chebi1, nct1, uberon1, uberon2}) {
            g.addVertex(v);
        }

        g.addEdge(cs1, cl1, edge("e1", cs1, cl1, "CS-CL"));
        g.addEdge(cl1, go1, edge("e2", cl1, go1, "CL-GO"));
        g.addEdge(cl1, cl2, edge("e3", cl1, cl2, "CL-CL", Map.of("Label", "SUB_CLASS_OF")));
        g.addEdge(go1, go2, edge("e4", go1, go2, "GO-GO", Map.of("Label", "SUB_CLASS_OF")));
        g.addEdge(chembl1, cl1, edge("e5", chembl1, cl1, "CHEMBL-CL"));
        g.addEdge(chembl1, pr1, edge("e6", chembl1, pr1, "CHEMBL-PR"));
        g.addEdge(cs1, chebi1, edge("e7", cs1, chebi1, "CS-CHEBI"));
        g.addEdge(chebi1, pr2, edge("e8", chebi1, pr2, "CHEBI-PR"));
        g.addEdge(cs1, nct1, edge("e9", cs1, nct1, "CS-NCT"));
        g.addEdge(cs1, uberon1, edge("e10", cs1, uberon1, "CS-UBERON"));
        g.addEdge(uberon1, uberon2, edge("e11", uberon1, uberon2, "UBERON-UBERON",
                Map.of("Label", "PART_OF")));

        return new TestGraph(g, cs1, cl1, cl2, go1, go2, chembl1, pr1, pr2, chebi1, nct1,
                uberon1, uberon2);
    }

    private Set<String> vertexIds(DirectedPseudograph<ArangoVertex, ArangoEdge> g) {
        return g.vertexSet().stream().map(ArangoVertex::id).collect(Collectors.toSet());
    }

    private Set<String> edgeKeys(DirectedPseudograph<ArangoVertex, ArangoEdge> g) {
        return g.edgeSet().stream().map(ArangoEdge::key).collect(Collectors.toSet());
    }

    // -- BFS tests --

    @Test
    void find_bidirectionalReachability() {
        TestGraph tg = buildTestGraph();
        // CHEMBL/1 is only reachable via incoming edge to CL/1; bidirectional BFS should find it
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        assertTrue(vertexIds(induced).contains("CHEMBL/1"),
                "CHEMBL/1 should be reachable via incoming edge from CL/1");
    }

    @Test
    void find_bidirectionalPreservesEdgeDirection() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        // The CHEMBL->PR edge should be present with correct direction
        assertTrue(edgeKeys(induced).contains("e6"), "CHEMBL-PR edge should be in induced subgraph");
        ArangoEdge e6 = induced.edgeSet().stream().filter(e -> e.key().equals("e6")).findFirst().orElseThrow();
        assertEquals("CHEMBL/1", e6.from());
        assertEquals("PR/1", e6.to());
    }

    @Test
    void find_ignoredCollections_chebiExcluded() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        assertFalse(vertexIds(induced).contains("CHEBI/1"), "CHEBI vertices should be excluded");
    }

    @Test
    void find_ignoredCollections_nctExcluded() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        assertFalse(vertexIds(induced).contains("NCT/1"), "NCT vertices should be excluded");
    }

    @Test
    void find_ignoredCollections_blocksTraversalBeyond() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        // PR/2 is only reachable through CHEBI/1, which is blocked
        assertFalse(vertexIds(induced).contains("PR/2"),
                "PR/2 should not be reachable since CHEBI is blocked");
    }

    @Test
    void find_selfReferentialEdgesSkippedInBfs() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        // CL/2, GO/2, UBERON/2 are only reachable via self-referential edges, so BFS should skip them
        assertFalse(vertexIds(induced).contains("CL/2"),
                "CL/2 should not be reached via CL-CL self-referential edge");
        assertFalse(vertexIds(induced).contains("GO/2"),
                "GO/2 should not be reached via GO-GO self-referential edge");
        assertFalse(vertexIds(induced).contains("UBERON/2"),
                "UBERON/2 should not be reached via UBERON-UBERON self-referential edge");
    }

    @Test
    void find_depthLimitRespected() {
        TestGraph tg = buildTestGraph();
        // With depth 1, only CS/1's direct neighbors via cross-collection edges should be found
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 1);
        Set<String> ids = vertexIds(induced);
        assertTrue(ids.contains("CS/1"));
        assertTrue(ids.contains("CL/1"), "CL/1 is 1 hop from CS/1");
        assertTrue(ids.contains("UBERON/1"), "UBERON/1 is 1 hop from CS/1");
        assertFalse(ids.contains("GO/1"), "GO/1 is 2 hops from CS/1");
    }

    @Test
    void find_depthBoundaryVertexIncluded() {
        TestGraph tg = buildTestGraph();
        // Depth 2: CS/1 -> CL/1 -> GO/1 (depth 2 should be included)
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 2);
        assertTrue(vertexIds(induced).contains("GO/1"), "GO/1 at depth 2 should be included");
    }

    @Test
    void find_allEdgesBetweenReachableVerticesIncluded() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        Set<String> keys = edgeKeys(induced);
        // Cross-collection edges between reachable vertices should all be present
        assertTrue(keys.contains("e1"), "CS-CL edge");
        assertTrue(keys.contains("e2"), "CL-GO edge");
        assertTrue(keys.contains("e5"), "CHEMBL-CL edge");
        assertTrue(keys.contains("e6"), "CHEMBL-PR edge");
        assertTrue(keys.contains("e10"), "CS-UBERON edge");
    }

    @Test
    void find_edgesToIgnoredCollectionsExcluded() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        Set<String> keys = edgeKeys(induced);
        assertFalse(keys.contains("e7"), "CS-CHEBI edge should be excluded");
        assertFalse(keys.contains("e8"), "CHEBI-PR edge should be excluded");
        assertFalse(keys.contains("e9"), "CS-NCT edge should be excluded");
    }

    // -- Hierarchy enrichment tests --

    @Test
    void addOntologyHierarchyPaths_walkAddsAncestors() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        finder.addOntologyHierarchyPaths(tg.graph(), induced);
        // GO/2 should now be added via walk from GO/1 following SUB_CLASS_OF
        assertTrue(vertexIds(induced).contains("GO/2"),
                "GO/2 should be added by hierarchy walk from GO/1");
        assertTrue(edgeKeys(induced).contains("e4"),
                "GO-GO SUB_CLASS_OF edge should be added by hierarchy enrichment");
    }

    @Test
    void addOntologyHierarchyPaths_walkAddsUberonAncestors() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        finder.addOntologyHierarchyPaths(tg.graph(), induced);
        assertTrue(vertexIds(induced).contains("UBERON/2"),
                "UBERON/2 should be added by hierarchy walk following PART_OF");
        assertTrue(edgeKeys(induced).contains("e11"),
                "UBERON-UBERON PART_OF edge should be added by hierarchy enrichment");
    }

    @Test
    void addOntologyHierarchyPaths_allStrategyIncludesEntireCollection() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        finder.addOntologyHierarchyPaths(tg.graph(), induced);
        // CL uses "all" strategy — both CL/1 and CL/2 should be present
        Set<String> ids = vertexIds(induced);
        assertTrue(ids.contains("CL/1"));
        assertTrue(ids.contains("CL/2"), "CL/2 should be included by 'all' strategy");
        assertTrue(edgeKeys(induced).contains("e3"), "CL-CL edge should be included by 'all' strategy");
    }

    @Test
    void addOntologyHierarchyPaths_noDuplicateVertices() {
        TestGraph tg = buildTestGraph();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(tg.graph(), "CS", 10);
        int verticesBefore = induced.vertexSet().size();
        // CL/1 and GO/1 are already in the induced subgraph
        finder.addOntologyHierarchyPaths(tg.graph(), induced);
        // Count CL/1 occurrences — should be exactly 1
        long cl1Count = induced.vertexSet().stream().filter(v -> v.id().equals("CL/1")).count();
        assertEquals(1, cl1Count, "CL/1 should not be duplicated");
    }
}
