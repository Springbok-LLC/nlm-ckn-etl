package gov.nih.nlm;

import com.arangodb.ArangoDB;
import com.arangodb.ArangoDatabase;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

import java.util.Map;

public class InducedSubgraphBuilder {

    private static final int MAX_DEPTH = 5;

    private final ArangoDatabase sourceDb;
    private final ArangoDatabase targetDb;

    /**
     * Creates a builder that reads from sourceDb and writes to targetDb.
     *
     * @param sourceDb the source ArangoDB database containing the full graph
     * @param targetDb the target ArangoDB database for the induced subgraph
     */
    public InducedSubgraphBuilder(ArangoDatabase sourceDb, ArangoDatabase targetDb) {
        this.sourceDb = sourceDb;
        this.targetDb = targetDb;
    }

    /**
     * Build an induced subgraph from sourceDb and write it to targetDb.
     *
     * @param graphName        the name of the source named graph
     * @param sourceCollection the vertex collection to start BFS from (e.g., "CS")
     * @param subgraphName     the name for the new named graph in targetDb
     * @param maxDepth         maximum BFS traversal depth
     */
    public void build(String graphName, String sourceCollection, String subgraphName, int maxDepth) {

        // Load the full graph from the source database
        System.out.println("Loading full graph via ArangoGraphLoader...");
        var loader = new ArangoGraphLoader(sourceDb);
        DirectedPseudograph<ArangoVertex, ArangoEdge> graph = loader.load(graphName);

        // Find induced subgraph via BFS
        System.out.println("Finding induced subgraph...");
        var finder = new InducedSubgraphFinder();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(graph, sourceCollection, maxDepth);

        // Add ontology hierarchy paths
        System.out.println("Adding ontology hierarchy paths...");
        finder.addOntologyHierarchyPaths(graph, induced);

        // Write to target database
        System.out.println("Writing subgraph to target database...");
        var writer = new ArangoGraphWriter(targetDb);
        writer.write(induced, subgraphName);
    }

    public static void main(String[] args) {
        Map<String, String> env = System.getenv();

        String host = env.getOrDefault("ARANGO_DB_HOST", "localhost");
        int port = Integer.parseInt(env.getOrDefault("ARANGO_DB_PORT", "8529"));
        String user = env.getOrDefault("ARANGO_DB_USER", "root");
        String password = env.getOrDefault("ARANGO_DB_PASSWORD", "");
        String ontologyDbName = env.getOrDefault("ARANGO_ONTOLOGY_DB_NAME", "");
        String phenotypeDbName = env.getOrDefault("ARANGO_PHENOTYPE_DB_NAME", "");
        String ontologyGraphName = env.getOrDefault("ARANGO_ONTOLOGY_GRAPH_NAME", "");
        String phenotypeGraphName = env.getOrDefault("ARANGO_PHENOTYPE_GRAPH_NAME", "");

        ArangoDB arango = new ArangoDB.Builder()
                .host(host, port)
                .user(user)
                .password(password)
                .build();

        // Source database (read-only)
        ArangoDatabase sourceDb = arango.db(ontologyDbName);

        // Drop and recreate target database
        if (arango.db(phenotypeDbName).exists()) {
            arango.db(phenotypeDbName).drop();
        }
        arango.createDatabase(phenotypeDbName);
        ArangoDatabase targetDb = arango.db(phenotypeDbName);

        // Drop existing graph in target if present
        if (targetDb.graph(phenotypeGraphName).exists()) {
            targetDb.graph(phenotypeGraphName).drop();
        }

        new InducedSubgraphBuilder(sourceDb, targetDb).build(
                ontologyGraphName,
                "CS",
                phenotypeGraphName,
                MAX_DEPTH
        );

        arango.shutdown();
    }
}
