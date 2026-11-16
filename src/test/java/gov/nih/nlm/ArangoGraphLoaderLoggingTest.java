package gov.nih.nlm;

import com.arangodb.ArangoCursor;
import com.arangodb.ArangoDatabase;
import com.arangodb.ArangoGraph;
import com.arangodb.entity.EdgeDefinition;
import com.arangodb.entity.GraphEntity;
import com.arangodb.util.RawJson;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Field;
import java.lang.reflect.Proxy;
import java.util.List;
import java.util.function.Consumer;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events ArangoGraphLoader logs, over a database that serves canned documents. */
class ArangoGraphLoaderLoggingTest {

    @SuppressWarnings("unchecked")
    private static ArangoCursor<RawJson> cursor(List<String> docs) {
        List<RawJson> raw = docs.stream().map(RawJson::of).toList();
        return (ArangoCursor<RawJson>) Proxy.newProxyInstance(ArangoGraphLoaderLoggingTest.class.getClassLoader(),
                new Class<?>[]{ArangoCursor.class}, (proxy, method, args) -> switch (method.getName()) {
                    case "iterator" -> raw.iterator();
                    case "forEach" -> {
                        raw.forEach((Consumer<RawJson>) args[0]);
                        yield null;
                    }
                    default -> null;
                });
    }

    /** A database with one graph, CL -> CL via CL-CL, whose collections hold the given documents. */
    private static ArangoDatabase database(List<String> vertices, List<String> edges) throws Exception {
        GraphEntity info = new GraphEntity();
        Field field = GraphEntity.class.getDeclaredField("edgeDefinitions");
        field.setAccessible(true);
        field.set(info, List.of(new EdgeDefinition().collection("CL-CL").from("CL").to("CL")));
        ArangoGraph graph = (ArangoGraph) Proxy.newProxyInstance(ArangoGraphLoaderLoggingTest.class.getClassLoader(),
                new Class<?>[]{ArangoGraph.class}, (proxy, method, args) -> "getInfo".equals(method.getName()) ? info : null);
        return (ArangoDatabase) Proxy.newProxyInstance(ArangoGraphLoaderLoggingTest.class.getClassLoader(),
                new Class<?>[]{ArangoDatabase.class}, (proxy, method, args) -> switch (method.getName()) {
                    case "graph" -> graph;
                    case "query" -> cursor(((String) args[0]).contains("`CL-CL`") ? edges : vertices);
                    default -> null;
                });
    }

    private static String vertex(String key) {
        return "{\"_id\":\"CL/" + key + "\",\"_key\":\"" + key + "\",\"_rev\":\"r\",\"label\":\"x\"}";
    }

    private static String edge(String key, String from, String to) {
        return "{\"_id\":\"CL-CL/" + key + "\",\"_key\":\"" + key + "\",\"_from\":\"CL/" + from + "\",\"_to\":\"CL/" + to + "\"}";
    }

    @Test
    void loadIsOneEventWithTheDanglingEdgesCountedAsRejected() throws Exception {
        ArangoDatabase db = database(List.of(vertex("1"), vertex("2")),
                List.of(edge("e1", "1", "2"), edge("e2", "1", "9")));
        try (LogCapture logs = new LogCapture()) {
            var graph = new ArangoGraphLoader(db).load("KN-Ontologies-v2.0");

            assertEquals(2, graph.vertexSet().size());
            LogCapture.Event e = logs.one("graph_loaded");
            assertEquals("INFO", e.level());
            assertEquals("KN-Ontologies-v2.0", e.get("graph"));
            assertEquals(2, e.get("records_out"));
            assertEquals(1, e.get("edges"));
            assertEquals(1, e.get("records_rejected"));
            assertTrue(e.get("duration_ms") instanceof Long);
        }
    }

    @Test
    void aMalformedDocumentNamesItsCollection() throws Exception {
        ArangoDatabase db = database(List.of("{not json"), List.of());

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> new ArangoGraphLoader(db).load("g"));

        assertTrue(e.getMessage().startsWith("Failed to deserialize vertex in CL: {not json"), e.getMessage());
    }
}
