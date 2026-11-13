package gov.nih.nlm;

import ch.qos.logback.classic.Level;
import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoGraph;
import com.arangodb.ArangoVertexCollection;
import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import org.apache.jena.graph.Node;
import org.apache.jena.graph.NodeFactory;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.net.URI;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events ResultsGraphBuilder logs. */
class ResultsGraphBuilderLoggingTest {

    private static final String OBO = "http://purl.obolibrary.org/obo/";
    private static final String SUB_CLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf";
    private static final String LABEL = "http://www.w3.org/2000/01/rdf-schema#label";

    private Object savedUtilities;

    @BeforeEach
    void useInMemoryUtilities() throws Exception {
        Field field = ResultsGraphBuilder.class.getDeclaredField("arangoDbUtilities");
        field.setAccessible(true);
        savedUtilities = field.get(null);
        field.set(null, new ArangoDbUtilities(Map.of("ARANGO_DB_HOST", "localhost", "ARANGO_DB_PORT", "8529",
                "ARANGO_DB_USER", "root", "ARANGO_DB_PASSWORD", "x")) {
            @Override
            public ArangoVertexCollection createOrGetVertexCollection(ArangoGraph graph, String vertexName) {
                return null;
            }

            @Override
            public ArangoEdgeCollection createOrGetEdgeCollection(ArangoGraph graph, String from, String to) {
                return null;
            }
        });
    }

    @AfterEach
    void restoreUtilities() throws Exception {
        Field field = ResultsGraphBuilder.class.getDeclaredField("arangoDbUtilities");
        field.setAccessible(true);
        field.set(null, savedUtilities);
    }

    private static ArrayList<Node> tuple(String... values) {
        ArrayList<Node> nodes = new ArrayList<>();
        for (String v : values) {
            nodes.add(v.startsWith("http") ? NodeFactory.createURI(v) : NodeFactory.createLiteral(v));
        }
        return nodes;
    }

    private static Map<String, OntologyElementMap> maps() {
        OntologyElementMap ro = new OntologyElementMap();
        ro.getTerms().put("RO_0000050", new OntologyElementMap.OntologyTerm(URI.create(OBO + "RO_0000050"), "part of"));
        return Map.of("ro", ro);
    }

    @Test
    void edgeConstructionCountsRejectsByReasonAndIsDebugPerFile() {
        ArrayList<ArrayList<Node>> tuples = new ArrayList<>(List.of(
                tuple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002"),
                tuple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "BFO_0000002"),
                tuple(OBO + "BFO_0000002", SUB_CLASS_OF, OBO + "CL_0000002"),
                tuple(OBO + "CL_0000001", OBO + "RO_0009999", OBO + "CL_0000002"),
                tuple(OBO + "CL_0000001", LABEL, "an attribute, not an edge"),
                tuple("http://example.org/unrecognized", LABEL, "an attribute of nothing")));
        Map<String, Integer> rejected = new TreeMap<>();
        try (LogCapture logs = new LogCapture(Level.DEBUG)) {
            ResultsGraphBuilder.constructEdges(tuples, maps(), null, new HashMap<>(), new HashMap<>(),
                    new HashMap<>(), rejected);

            assertEquals(Map.of("invalid_vertex", 3, "no_label", 1), rejected);
            LogCapture.Event e = logs.one("edges_constructed");
            assertEquals("DEBUG", e.level());
            assertEquals(6, e.get("records_in"));
            assertEquals(1, e.get("records_out"));
        }
        try (LogCapture logs = new LogCapture()) {
            ResultsGraphBuilder.constructEdges(tuples, maps(), null, new HashMap<>(), new HashMap<>(),
                    new HashMap<>(), new TreeMap<>());
            assertTrue(logs.events().isEmpty());
        }
    }

    @Test
    void edgeUpdateCountsInvalidQuintuples() {
        ArrayList<ArrayList<Node>> tuples = new ArrayList<>(List.of(
                tuple(OBO + "BFO_0000002", SUB_CLASS_OF, OBO + "CL_0000002", LABEL, "x")));
        Map<String, Integer> rejected = new TreeMap<>();
        ResultsGraphBuilder.updateEdges(tuples, maps(), new HashMap<>(), rejected);
        assertEquals(Map.of("invalid_vertex", 1), rejected);
    }

    @Test
    void tuplesFileIsOneEventWithItsRejectReasons() {
        ArrayList<ArrayList<Node>> tuples = new ArrayList<>(List.of(
                tuple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002"),
                tuple(OBO + "CL_0000001", SUB_CLASS_OF),
                tuple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002", LABEL, "x")));
        Map<String, Integer> rejected = new TreeMap<>(Map.of("no_label", 2));
        try (LogCapture logs = new LogCapture()) {
            ResultsGraphBuilder.logTuplesFile(Path.of("/tuples/a.json"), tuples, rejected, System.nanoTime());

            LogCapture.Event e = logs.one("tuples_file_processed");
            assertEquals("INFO", e.level());
            assertEquals("/tuples/a.json", e.get("tuples_file"));
            assertEquals(3, e.get("records_in"));
            assertEquals(1L, e.get("triples"));
            assertEquals(1L, e.get("quintuples"));
            assertEquals(3, e.get("records_rejected"));
            assertEquals(Map.of("no_label", 2, "not_a_triple", 1), e.get("rejected_by_reason"));
        }
    }

    @Test
    void aCleanFileHasNoRejectedByReasonField() {
        ArrayList<ArrayList<Node>> tuples = new ArrayList<>(List.of(
                tuple(OBO + "CL_0000001", SUB_CLASS_OF, OBO + "CL_0000002")));
        try (LogCapture logs = new LogCapture()) {
            ResultsGraphBuilder.logTuplesFile(Path.of("a.json"), tuples, new TreeMap<>(), System.nanoTime());
            LogCapture.Event e = logs.one("tuples_file_processed");
            assertEquals(0, e.get("records_rejected"));
            assertFalse(e.fields().containsKey("rejected_by_reason"));
        }
    }

    @Test
    void updatingAVertexThatWasNeverBuiltStillFailsLoudly() {
        ArrayList<ArrayList<Node>> tuples = new ArrayList<>(List.of(
                tuple(OBO + "CL_0000001", LABEL, "a cell")));
        assertThrows(RuntimeException.class,
                () -> ResultsGraphBuilder.updateVertices(tuples, maps(), new HashMap<String, Map<String, BaseDocument>>(
                        Map.of("CL", new HashMap<>()))));
    }
}
