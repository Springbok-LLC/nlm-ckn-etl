package gov.nih.nlm;

import com.arangodb.ArangoDB;
import com.arangodb.ArangoDatabase;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

public class InducedSubgraphBuilder {

    private final ArangoDatabase db;

    public InducedSubgraphBuilder(ArangoDatabase db) {
        this.db = db;
    }

    public static void main(String[] args) {
        ArangoDB arango = new ArangoDB.Builder().host("localhost", 8529).password("pass").build();

        ArangoDatabase db = arango.db("myDatabase");

        new InducedSubgraphBuilder(db).build("myNamedGraph",
                "myVertexCollection",
                "myInducedSubgraph",
                10  // max BFS depth
        );

        arango.shutdown();
    }

    public void build(String graphName, String sourceCollection, String subgraphName, int maxDepth) {

        System.out.println("Loading graph from ArangoDB...");
        var loader = new ArangoGraphLoader(db);
        DirectedPseudograph<ArangoVertex, ArangoEdge> graph = loader.load(graphName);

        System.out.println("Finding induced subgraph...");
        var finder = new InducedSubgraphFinder();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(graph, sourceCollection, maxDepth);

        System.out.println("Writing subgraph to ArangoDB...");
        var writer = new ArangoGraphWriter(db);
        writer.write(induced, subgraphName);
    }
}
