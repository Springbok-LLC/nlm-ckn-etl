package gov.nih.nlm;

import ch.qos.logback.classic.Level;
import com.arangodb.ArangoCollection;
import com.arangodb.ArangoDatabase;
import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoGraph;
import com.arangodb.ArangoVertexCollection;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events the ArangoDB classes and the induced subgraph classes log, over in-memory stand-ins. */
class ArangoAndInducedLoggingTest {

    private static final Map<String, String> ENV = Map.of("ARANGO_DB_HOST", "localhost", "ARANGO_DB_PORT", "8529",
            "ARANGO_DB_USER", "root", "ARANGO_DB_PASSWORD", "s3cret");

    @SuppressWarnings("unchecked")
    private static <T> T stub(Class<T> type, java.util.function.BiFunction<String, Object[], Object> handler) {
        return (T) Proxy.newProxyInstance(ArangoAndInducedLoggingTest.class.getClassLoader(), new Class<?>[]{type},
                (proxy, method, args) -> handler.apply(method.getName(), args == null ? new Object[0] : args));
    }

    /** A graph that exists or not, with named vertex collections, and a named graph. */
    private static ArangoGraph graph(boolean exists, List<String> vertexCollections, List<String> edgeDefinitions) {
        ArangoVertexCollection vertexCollection = stub(ArangoVertexCollection.class, (m, a) -> null);
        ArangoEdgeCollection edgeCollection = stub(ArangoEdgeCollection.class, (m, a) -> null);
        return stub(ArangoGraph.class, (m, a) -> switch (m) {
            case "exists" -> exists;
            case "name" -> "g";
            case "getVertexCollections" -> vertexCollections;
            case "getEdgeDefinitions" -> edgeDefinitions;
            case "vertexCollection" -> vertexCollection;
            case "edgeCollection" -> edgeCollection;
            default -> null;
        });
    }

    private static ArangoDatabase database(ArangoGraph graph) {
        return stub(ArangoDatabase.class, (m, a) -> switch (m) {
            case "name" -> "db";
            case "graph" -> graph;
            default -> null;
        });
    }

    // --- ArangoDbUtilities ---

    @Test
    void clientCreationLogsTheTargetAndNeverThePassword() {
        try (LogCapture logs = new LogCapture()) {
            new ArangoDbUtilities(ENV);

            LogCapture.Event e = logs.one("arangodb_client_created");
            assertEquals("localhost", e.get("host"));
            assertEquals(8529, e.get("port"));
            assertEquals(false, e.get("ssl"));
            assertEquals("root", e.get("user"));
            assertFalse(e.fields().values().contains("s3cret"));
        }
    }

    @Test
    void missingHostOrPortIsLoggedThenFails() {
        try (LogCapture logs = new LogCapture()) {
            IllegalStateException e = assertThrows(IllegalStateException.class,
                    () -> new ArangoDbUtilities(Map.of("ARANGO_DB_PORT", "8529")));
            assertEquals("ARANGO_DB_HOST is not set", e.getMessage());
            assertEquals("ARANGO_DB_HOST", logs.one("arangodb_config_missing").get("variable"));
            assertEquals("ERROR", logs.one("arangodb_config_missing").level());
        }
    }

    @Test
    void graphCreatedIsInfoAndDroppedIsWarn() {
        ArangoDbUtilities utilities = new ArangoDbUtilities(ENV);
        try (LogCapture logs = new LogCapture()) {
            utilities.createOrGetGraph(database(graph(false, List.of(), List.of())), "g");
            LogCapture.Event created = logs.one("graph_created");
            assertEquals("INFO", created.level());
            assertEquals("db", created.get("database"));
            assertEquals("g", created.get("graph"));

            utilities.deleteGraph(database(graph(true, List.of(), List.of())), "g");
            assertEquals("WARN", logs.one("graph_dropped").level());
        }
    }

    @Test
    void existingGraphIsNotLogged() {
        ArangoDbUtilities utilities = new ArangoDbUtilities(ENV);
        try (LogCapture logs = new LogCapture()) {
            utilities.createOrGetGraph(database(graph(true, List.of(), List.of())), "g");
            assertTrue(logs.events().isEmpty());
        }
    }

    @Test
    void collectionCreationIsDebugAndDeletionIsWarn() {
        ArangoDbUtilities utilities = new ArangoDbUtilities(ENV);
        try (LogCapture logs = new LogCapture(Level.DEBUG)) {
            utilities.createOrGetVertexCollection(graph(true, List.of(), List.of()), "CL");
            utilities.createOrGetEdgeCollection(graph(true, List.of(), List.of()), "CL", "UBERON");
            assertEquals("DEBUG", logs.one("vertex_collection_created").level());
            assertEquals("CL-UBERON", logs.one("edge_definition_created").get("collection"));
            assertEquals("g", logs.one("edge_definition_created").get("graph"));

            utilities.deleteVertexCollection(graph(true, List.of("CL"), List.of()), "CL");
            utilities.deleteEdgeCollection(graph(true, List.of(), List.of("CL-UBERON")), "CL-UBERON");
            assertEquals("WARN", logs.one("vertex_collection_deleted").level());
            assertEquals("WARN", logs.one("edge_definition_deleted").level());
        }
        try (LogCapture logs = new LogCapture()) {
            utilities.createOrGetVertexCollection(graph(true, List.of(), List.of()), "CL");
            assertTrue(logs.events().isEmpty());
        }
    }

    // --- ArangoGraphWriter ---

    private static ArangoVertex vertex(String collection, String key) {
        return new ArangoVertex(collection + "/" + key, collection, key, Map.of());
    }

    private static ArangoEdge edge(String key, ArangoVertex from, ArangoVertex to, String collection) {
        return new ArangoEdge(key, from.id(), to.id(), collection, Map.of());
    }

    @Test
    void writerLogsOneEventWithWhatItWrote() {
        ArangoVertex cs1 = vertex("CS", "1");
        ArangoVertex uberon1 = vertex("UBERON", "1");
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = new DirectedPseudograph<>(ArangoEdge.class);
        induced.addVertex(cs1);
        induced.addVertex(uberon1);
        induced.addEdge(cs1, uberon1, edge("e1", cs1, uberon1, "CS-UBERON"));
        ArangoCollection collection = stub(ArangoCollection.class, (m, a) -> null);
        ArangoDatabase db = stub(ArangoDatabase.class, (m, a) -> switch (m) {
            case "name" -> "db";
            case "collection" -> collection;
            default -> null;
        });
        try (LogCapture logs = new LogCapture()) {
            new ArangoGraphWriter(db).write(induced, "phenotypes");

            LogCapture.Event e = logs.one("induced_graph_created");
            assertEquals("phenotypes", e.get("graph"));
            assertEquals("db", e.get("database"));
            assertEquals(2, e.get("vertices_written"));
            assertEquals(1, e.get("edges_written"));
            assertEquals(2, e.get("vertex_collection_count"));
            assertEquals(1, e.get("edge_collection_count"));
            assertTrue(e.get("duration_ms") instanceof Long);
        }
    }

    // --- InducedSubgraphFinder ---

    private static DirectedPseudograph<ArangoVertex, ArangoEdge> sourceGraph() {
        ArangoVertex cs1 = vertex("CS", "1");
        ArangoVertex uberon1 = vertex("UBERON", "1");
        ArangoVertex uberon2 = vertex("UBERON", "2");
        ArangoVertex pr1 = vertex("PR", "1");
        DirectedPseudograph<ArangoVertex, ArangoEdge> graph = new DirectedPseudograph<>(ArangoEdge.class);
        List.of(cs1, uberon1, uberon2, pr1).forEach(graph::addVertex);
        graph.addEdge(cs1, uberon1, edge("e1", cs1, uberon1, "CS-UBERON"));
        graph.addEdge(cs1, pr1, edge("e2", cs1, pr1, "CS-PR"));
        graph.addEdge(uberon1, uberon2,
                new ArangoEdge("e3", uberon1.id(), uberon2.id(), "UBERON-UBERON", Map.of("Label", "PART_OF")));
        return graph;
    }

    @Test
    void extractionIsOneEventWithItsCounts() {
        try (LogCapture logs = new LogCapture()) {
            var induced = new InducedSubgraphFinder().find(sourceGraph(), "CS", 1);

            LogCapture.Event e = logs.one("induced_subgraph_extracted");
            assertEquals("INFO", e.level());
            assertEquals(4, e.get("records_in"));
            assertEquals("CS", e.get("source_collection"));
            assertEquals(1, e.get("source_vertices"));
            assertEquals(induced.vertexSet().size(), e.get("reachable_vertices"));
            assertEquals(induced.edgeSet().size(), e.get("records_out"));
            assertEquals(1, e.get("max_depth"));
            assertTrue(logs.named("induced_subgraph_empty").isEmpty());
        }
    }

    @Test
    void noSourceVerticesWarns() {
        try (LogCapture logs = new LogCapture()) {
            new InducedSubgraphFinder().find(sourceGraph(), "NOPE", 1);

            LogCapture.Event e = logs.one("induced_subgraph_empty");
            assertEquals("WARN", e.level());
            assertEquals("no_source_vertices", e.get("reason"));
            assertEquals(0, logs.one("induced_subgraph_extracted").get("source_vertices"));
        }
    }

    @Test
    void hierarchyEnrichmentIsOneEventPerPrefixAndSkipsAreDebug() {
        var full = sourceGraph();
        var induced = new InducedSubgraphFinder().find(full, "CS", 1);
        try (LogCapture logs = new LogCapture(Level.DEBUG)) {
            new InducedSubgraphFinder().addOntologyHierarchyPaths(full, induced);

            LogCapture.Event e = logs.one("hierarchy_enriched");
            assertEquals("UBERON", e.get("prefix"));
            assertEquals("walk", e.get("strategy"));
            assertEquals(1L, e.get("vertices_added"));
            assertEquals(1L, e.get("edges_added"));
            LogCapture.Event skipped = logs.one("hierarchy_skipped");
            assertEquals("DEBUG", skipped.level());
            assertEquals("PR", skipped.get("prefix"));
        }
    }

    @Test
    void hierarchyEnrichmentCountsOnlyWhatWasNotAlreadyThere() {
        var full = sourceGraph();
        var induced = new InducedSubgraphFinder().find(full, "CS", 1);
        new InducedSubgraphFinder().addOntologyHierarchyPaths(full, induced);
        try (LogCapture logs = new LogCapture()) {
            new InducedSubgraphFinder().addOntologyHierarchyPaths(full, induced);

            LogCapture.Event e = logs.one("hierarchy_enriched");
            assertEquals(0L, e.get("vertices_added"));
            assertEquals(0L, e.get("edges_added"));
        }
    }

    // --- InducedSubgraphBuilder ---

    @Test
    void emptyDatabaseOrGraphNamesFailBeforeAnythingIsDropped() {
        try (LogCapture logs = new LogCapture()) {
            IllegalStateException e = assertThrows(IllegalStateException.class,
                    () -> InducedSubgraphBuilder.run(Map.of()));
            assertEquals("ARANGO_ONTOLOGY_DB_NAME is not set", e.getMessage());
            assertEquals("ARANGO_ONTOLOGY_DB_NAME", logs.one("arangodb_config_missing").get("variable"));
            assertTrue(logs.named("database_dropped").isEmpty());
        }
    }
}
