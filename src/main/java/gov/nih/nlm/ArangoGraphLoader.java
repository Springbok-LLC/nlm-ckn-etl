package gov.nih.nlm;

import com.arangodb.ArangoDatabase;
import com.arangodb.util.RawJson;
import com.fasterxml.jackson.databind.ObjectMapper;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public class ArangoGraphLoader {

    private final ArangoDatabase db;
    private final ObjectMapper mapper = new ObjectMapper();

    /**
     * Creates a loader for the given ArangoDB database.
     *
     * @param db the ArangoDB database connection to load graphs from
     */
    public ArangoGraphLoader(ArangoDatabase db) {
        this.db = db;
    }

    /**
     * Load a named graph from ArangoDB into a JGraphT directed pseudograph.
     *
     * @param graphName the name of the ArangoDB named graph to load
     * @return the graph as a JGraphT {@link DirectedPseudograph}
     */
    public DirectedPseudograph<ArangoVertex, ArangoEdge> load(String graphName) {

        DirectedPseudograph<ArangoVertex, ArangoEdge> graph = new DirectedPseudograph<>(ArangoEdge.class);

        // Get all vertex and edge collections from the named graph
        var graphInfo = db.graph(graphName).getInfo();
        var edgeDefs = graphInfo.getEdgeDefinitions();

        Set<String> vertexCollections = new HashSet<>();
        List<String> edgeCollections = new ArrayList<>();

        for (var ed : edgeDefs) {
            edgeCollections.add(ed.getCollection());
            vertexCollections.addAll(ed.getFrom());
            vertexCollections.addAll(ed.getTo());
        }

        // Load all vertices, keyed by _id for edge lookup
        Map<String, ArangoVertex> vertexIndex = new HashMap<>();

        for (String col : vertexCollections) {
            String aql = "FOR v IN `%s` RETURN v".formatted(col);

            db.query(aql, RawJson.class).forEach(raw -> {
                try {
                    @SuppressWarnings("unchecked") Map<String, Object> doc = mapper.readValue(raw.get(), Map.class);

                    String id = (String) doc.get("_id");
                    String key = (String) doc.get("_key");

                    // Strip system fields from properties
                    Map<String, Object> props = stripSystemFields(doc);

                    ArangoVertex vertex = new ArangoVertex(id, col, key, props);

                    vertexIndex.put(id, vertex);
                    graph.addVertex(vertex);

                } catch (Exception e) {
                    throw new RuntimeException("Failed to deserialize vertex", e);
                }
            });
        }

        // Load all edges
        for (String col : edgeCollections) {
            String aql = "FOR e IN `%s` RETURN e".formatted(col);

            db.query(aql, RawJson.class).forEach(raw -> {
                try {
                    @SuppressWarnings("unchecked") Map<String, Object> doc = mapper.readValue(raw.get(), Map.class);

                    String key = (String) doc.get("_key");
                    String from = (String) doc.get("_from");
                    String to = (String) doc.get("_to");

                    ArangoVertex fromVertex = vertexIndex.get(from);
                    ArangoVertex toVertex = vertexIndex.get(to);

                    // Skip edges with endpoints outside the graph
                    if (fromVertex == null || toVertex == null) return;

                    Map<String, Object> props = stripSystemFields(doc);
                    ArangoEdge edge = new ArangoEdge(key, from, to, col, props);
                    graph.addEdge(fromVertex, toVertex, edge);

                } catch (Exception e) {
                    throw new RuntimeException("Failed to deserialize edge", e);
                }
            });
        }

        return graph;
    }

    private Map<String, Object> stripSystemFields(Map<String, Object> doc) {
        Map<String, Object> props = new HashMap<>(doc);
        props.remove("_id");
        props.remove("_key");
        props.remove("_rev");
        props.remove("_from");
        props.remove("_to");
        return props;
    }
}
