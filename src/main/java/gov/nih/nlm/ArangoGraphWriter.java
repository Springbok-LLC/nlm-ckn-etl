package gov.nih.nlm;

import com.arangodb.ArangoDatabase;
import com.arangodb.entity.CollectionType;
import com.arangodb.entity.EdgeDefinition;
import com.arangodb.model.CollectionCreateOptions;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

public class ArangoGraphWriter {

    private final ArangoDatabase db;

    /**
     * Creates a writer for the given ArangoDB database.
     *
     * @param db the target ArangoDB database connection to write graphs to
     */
    public ArangoGraphWriter(ArangoDatabase db) {
        this.db = db;
    }

    /**
     * Write the induced subgraph to the target database, preserving
     * original vertex and edge collection names.
     *
     * @param induced      the induced subgraph to write
     * @param subgraphName the name for the new named graph
     */
    public void write(DirectedPseudograph<ArangoVertex, ArangoEdge> induced, String subgraphName) {

        // Discover vertex collections
        Set<String> vertexCollections = induced.vertexSet().stream()
                .map(ArangoVertex::collection)
                .collect(Collectors.toSet());

        // Discover edge collections
        Set<String> edgeCollections = induced.edgeSet().stream()
                .map(ArangoEdge::collection)
                .collect(Collectors.toSet());

        // Create vertex collections
        System.out.println("Creating collections...");
        for (String col : vertexCollections) {
            db.createCollection(col);
        }

        // Create edge collections
        for (String col : edgeCollections) {
            db.createCollection(col, new CollectionCreateOptions().type(CollectionType.EDGES));
        }

        // Insert vertices
        System.out.println("Inserting vertices...");
        for (ArangoVertex vertex : induced.vertexSet()) {
            Map<String, Object> doc = new HashMap<>(vertex.properties());
            doc.put("_key", vertex.key());
            db.collection(vertex.collection()).insertDocument(doc);
        }

        // Insert edges
        System.out.println("Inserting edges...");
        for (ArangoEdge edge : induced.edgeSet()) {
            Map<String, Object> doc = new HashMap<>(edge.properties());
            doc.put("_key", edge.key());
            doc.put("_from", edge.from());
            doc.put("_to", edge.to());
            db.collection(edge.collection()).insertDocument(doc);
        }

        // Register as a named graph
        System.out.println("Registering named graph...");
        List<String> allVertexCols = new ArrayList<>(vertexCollections);
        List<EdgeDefinition> edgeDefs = new ArrayList<>();

        for (String edgeCol : edgeCollections) {
            edgeDefs.add(new EdgeDefinition()
                    .collection(edgeCol)
                    .from(allVertexCols.toArray(new String[0]))
                    .to(allVertexCols.toArray(new String[0])));
        }

        db.createGraph(subgraphName, edgeDefs);

        System.out.println("Named graph '" + subgraphName + "' created in database '" + db.name() + "'.");
        System.out.println("  Vertex collections: " + allVertexCols);
        System.out.println("  Edge collections:   " + edgeCollections);
    }
}
