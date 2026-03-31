package gov.nih.nlm;

import com.arangodb.ArangoDatabase;
import com.arangodb.entity.CollectionType;
import com.arangodb.entity.EdgeDefinition;
import com.arangodb.model.CollectionCreateOptions;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

public class ArangoGraphWriter {

    private final ArangoDatabase db;

    public ArangoGraphWriter(ArangoDatabase db) {
        this.db = db;
    }

    public void write(DirectedPseudograph<ArangoVertex, ArangoEdge> induced, String subgraphName) {

        // Step 1 — Discover original collections from vertex IDs
        Map<String, String> vertexColMap = induced.vertexSet().stream().map(ArangoVertex::collection).distinct().collect(
                Collectors.toMap(col -> col, col -> subgraphName + "_" + col));

        String newEdgeCol = subgraphName + "_edges";

        // Step 2 — Drop and recreate collections
        for (String newCol : vertexColMap.values()) {
            var col = db.collection(newCol);
            if (col.exists()) col.drop();
            db.createCollection(newCol);
        }

        var edgeCollection = db.collection(newEdgeCol);
        if (edgeCollection.exists()) edgeCollection.drop();
        db.createCollection(newEdgeCol, new CollectionCreateOptions().type(CollectionType.EDGES));

        // Step 3 — Insert vertices into their respective new collections
        for (ArangoVertex vertex : induced.vertexSet()) {
            String newCol = vertexColMap.get(vertex.collection());
            Map<String, Object> doc = new HashMap<>(vertex.properties());
            doc.put("_key", vertex.key());
            db.collection(newCol).insertDocument(doc);
        }

        // Step 4 — Insert edges, rewriting _from/_to to new collections
        for (ArangoEdge edge : induced.edgeSet()) {
            String fromCol = edge.from().split("/")[0];
            String fromKey = edge.from().split("/")[1];
            String toCol = edge.to().split("/")[0];
            String toKey = edge.to().split("/")[1];

            String newFrom = vertexColMap.get(fromCol) + "/" + fromKey;
            String newTo = vertexColMap.get(toCol) + "/" + toKey;

            Map<String, Object> doc = new HashMap<>(edge.properties());
            doc.put("_key", edge.key());
            doc.put("_from", newFrom);
            doc.put("_to", newTo);

            db.collection(newEdgeCol).insertDocument(doc);
        }

        // Step 5 — Register as a named graph
        List<String> newVertexCols = new ArrayList<>(vertexColMap.values());

        if (db.graph(subgraphName).exists()) {
            db.graph(subgraphName).drop();
        }

        EdgeDefinition edgeDef = new EdgeDefinition().collection(newEdgeCol).from(newVertexCols.toArray(new String[0])).to(
                newVertexCols.toArray(new String[0]));

        db.createGraph(subgraphName, Collections.singletonList(edgeDef));

        System.out.println("Named graph '" + subgraphName + "' created.");
        System.out.println("  Vertex collections: " + newVertexCols);
        System.out.println("  Edge collection:    " + newEdgeCol);
    }
}
