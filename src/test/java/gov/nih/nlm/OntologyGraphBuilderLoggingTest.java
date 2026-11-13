package gov.nih.nlm;

import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoGraph;
import com.arangodb.ArangoVertexCollection;
import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import org.apache.jena.graph.NodeFactory;
import org.apache.jena.graph.Triple;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.lang.reflect.Proxy;
import java.net.URI;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events OntologyGraphBuilder logs. */
class OntologyGraphBuilderLoggingTest {

    private static final String OBO = "http://purl.obolibrary.org/obo/";
    private static final String SUB_CLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf";
    private static final String LABEL = "http://www.w3.org/2000/01/rdf-schema#label";

    private static Triple triple(String s, String p, String o) {
        return Triple.create(NodeFactory.createURI(s), NodeFactory.createURI(p), NodeFactory.createURI(o));
    }

    private static Map<String, OntologyElementMap> maps() {
        OntologyElementMap ro = new OntologyElementMap();
        ro.getTerms().put("RO_0000050", new OntologyElementMap.OntologyTerm(URI.create(OBO + "RO_0000050"), "part of"));
        ro.getTerms().put("RO_0002160", new OntologyElementMap.OntologyTerm(URI.create(OBO + "RO_0002160"),
                "only in taxon"));
        return Map.of("ro", ro);
    }

    /** Utilities whose collections are in memory, so no ArangoDB is needed. */
    private static ArangoDbUtilities utilities() {
        return new ArangoDbUtilities(Map.of("ARANGO_DB_HOST", "localhost", "ARANGO_DB_PORT", "8529",
                "ARANGO_DB_USER", "root", "ARANGO_DB_PASSWORD", "x")) {
            @Override
            public ArangoVertexCollection createOrGetVertexCollection(ArangoGraph graph, String vertexName) {
                return null;
            }

            @Override
            public ArangoEdgeCollection createOrGetEdgeCollection(ArangoGraph graph, String from, String to) {
                return null;
            }
        };
    }

    @Test
    void verticesAreCountedAndInvalidIdsAreFiltered() {
        HashSet<Triple> triples = new HashSet<>(Set.of(
                triple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002"),
                triple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "BFO_0000002")));
        try (LogCapture logs = new LogCapture()) {
            OntologyGraphBuilder.constructVertices(triples, utilities(), null, new HashMap<>(), new HashMap<>());

            LogCapture.Event e = logs.one("vertices_constructed");
            assertEquals("INFO", e.level());
            assertEquals(2, e.get("records_in"));
            assertEquals(2, e.get("records_out"));
            assertEquals(1, e.get("records_rejected"));
            LogCapture.Event filtered = logs.one("triples_filtered");
            assertEquals("vertices", filtered.get("stage"));
            assertEquals("invalid_vertex", filtered.get("reason"));
            assertEquals(1, filtered.get("records_rejected"));
        }
    }

    @Test
    void verticesUpdatedCountsTheVerticesGivenAttributes() {
        Map<String, Map<String, BaseDocument>> docs = new HashMap<>();
        docs.put("CL", new HashMap<>(Map.of("0000001", new BaseDocument("0000001"))));
        HashSet<Triple> triples = new HashSet<>(Set.of(
                Triple.create(NodeFactory.createURI(OBO + "CL_0000001"), NodeFactory.createURI(LABEL),
                        NodeFactory.createLiteralString("a cell")),
                triple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002")));
        try (LogCapture logs = new LogCapture()) {
            OntologyGraphBuilder.updateVertices(triples, maps(), docs);

            LogCapture.Event e = logs.one("vertices_updated");
            assertEquals(2, e.get("records_in"));
            assertEquals(1, e.get("records_out"));
        }
    }

    @Test
    void edgesCountWhatWasFilteredByReason() {
        HashSet<Triple> triples = new HashSet<>(Set.of(
                triple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002"),
                triple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "BFO_0000002"),
                triple(OBO + "CL_0000001", OBO + "RO_0002160", OBO + "NCBITaxon_9606"),
                triple(OBO + "UBERON_0000001", OBO + "RO_0000050", OBO + "CL_0000002"),
                Triple.create(NodeFactory.createURI(OBO + "CL_0000001"), NodeFactory.createURI(LABEL),
                        NodeFactory.createLiteralString("a literal is an attribute, not a rejected edge"))));
        try (LogCapture logs = new LogCapture()) {
            OntologyGraphBuilder.constructEdges(triples, maps(), utilities(), null, new HashMap<>(), new HashMap<>());

            LogCapture.Event e = logs.one("edges_constructed");
            assertEquals(5, e.get("records_in"));
            assertEquals(1, e.get("records_out"));
            assertEquals(3, e.get("records_rejected"));
            Map<Object, Object> byReason = new HashMap<>();
            logs.named("triples_filtered").forEach(f -> byReason.put(f.get("reason"), f.get("records_rejected")));
            assertEquals(Map.of("invalid_vertex", 1, "taxon_constraint", 1, "label_filtered", 1), byReason);
            assertTrue(logs.named("triples_filtered").stream().allMatch(f -> "edges".equals(f.get("stage"))));

            LogCapture.Event dropped = logs.one("edges_dropped_by_label_filter");
            assertEquals("WARN", dropped.level());
            assertEquals("UBERON-CL", dropped.get("edge_collection"));
            assertEquals(1L, dropped.get("records_rejected"));
            assertEquals(Map.of("PART_OF", 1L), dropped.get("label_counts"));
        }
    }

    // --- inserts, over in-memory collections ---

    private static ArangoVertexCollection vertexCollection(Set<String> failKeys) {
        return (ArangoVertexCollection) Proxy.newProxyInstance(
                OntologyGraphBuilderLoggingTest.class.getClassLoader(), new Class<?>[]{ArangoVertexCollection.class},
                (proxy, method, args) -> switch (method.getName()) {
                    case "getVertex" -> null;
                    case "insertVertex" -> {
                        if (failKeys.contains(((BaseDocument) args[0]).getKey())) {
                            throw new RuntimeException("insert refused");
                        }
                        yield null;
                    }
                    default -> throw new UnsupportedOperationException(method.getName());
                });
    }

    private static Map<String, Map<String, BaseDocument>> vertexDocs(int n) {
        Map<String, BaseDocument> byNumber = new LinkedHashMap<>();
        for (int i = 0; i < n; i++) {
            String key = String.format("%07d", i);
            byNumber.put(key, new BaseDocument(key));
        }
        return Map.of("CL", byNumber);
    }

    @Test
    void insertedVerticesAreOneSummary(@TempDir Path dir) throws Exception {
        try (LogCapture logs = new LogCapture()) {
            OntologyGraphBuilder.insertVertices(Map.of("CL", vertexCollection(Set.of())), vertexDocs(3),
                    dir.resolve("deprecated.txt"));

            LogCapture.Event e = logs.one("vertices_inserted");
            assertEquals(3, e.get("records_in"));
            assertEquals(3, e.get("records_out"));
            assertEquals(0, e.get("records_rejected"));
            assertFalse(e.fields().containsKey("deprecated_terms_file"));
            assertTrue(logs.named("insert_failed").isEmpty());
        }
    }

    @Test
    void failedInsertsAreLoggedFirstFewOnlyThenCountedInTheSummary(@TempDir Path dir) {
        Set<String> failing = new HashSet<>();
        for (int i = 0; i < 8; i++) {
            failing.add(String.format("%07d", i));
        }
        try (LogCapture logs = new LogCapture()) {
            assertThrows(IllegalStateException.class, () -> OntologyGraphBuilder.insertVertices(
                    Map.of("CL", vertexCollection(failing)), vertexDocs(8), dir.resolve("deprecated.txt")));

            assertEquals(5, logs.named("insert_failed").size());
            LogCapture.Event first = logs.named("insert_failed").get(0);
            assertEquals("ERROR", first.level());
            assertEquals("insert", first.get("operation"));
            assertEquals("CL", first.get("collection"));
            assertEquals("insert refused", first.get("error"));
            assertEquals(8, logs.one("vertices_inserted").get("records_rejected"));
        }
    }
}
