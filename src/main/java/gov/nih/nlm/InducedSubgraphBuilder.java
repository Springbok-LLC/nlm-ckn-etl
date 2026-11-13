package gov.nih.nlm;

import com.arangodb.ArangoDB;
import com.arangodb.ArangoDatabase;
import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;

public class InducedSubgraphBuilder {

    private static final Logger LOG = LoggerFactory.getLogger(InducedSubgraphBuilder.class);

    private static final int MAX_DEPTH = 10;

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
        var loader = new ArangoGraphLoader(sourceDb);
        DirectedPseudograph<ArangoVertex, ArangoEdge> graph = loader.load(graphName);

        // Find induced subgraph via BFS
        var finder = new InducedSubgraphFinder();
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = finder.find(graph, sourceCollection, maxDepth);

        // Add ontology hierarchy paths
        finder.addOntologyHierarchyPaths(graph, induced);

        // Write to target database
        var writer = new ArangoGraphWriter(targetDb);
        writer.write(induced, subgraphName);
    }

    public static void main(String[] args) {
        RunLog.run("InducedSubgraphBuilder", () -> run(System.getenv()), "max_depth", MAX_DEPTH);
    }

    static void run(Map<String, String> env) {
        String host = env.getOrDefault("ARANGO_DB_HOST", "localhost");
        int port = Integer.parseInt(env.getOrDefault("ARANGO_DB_PORT", "8529"));
        String user = env.getOrDefault("ARANGO_DB_USER", "root");
        String password = env.getOrDefault("ARANGO_DB_PASSWORD", "");
        String ontologyDbName = requireName(env, "ARANGO_ONTOLOGY_DB_NAME");
        String phenotypeDbName = requireName(env, "ARANGO_PHENOTYPE_DB_NAME");
        String ontologyGraphName = requireName(env, "ARANGO_ONTOLOGY_GRAPH_NAME");
        String phenotypeGraphName = requireName(env, "ARANGO_PHENOTYPE_GRAPH_NAME");

        boolean ssl = ArangoDbUtilities.useSsl(env);
        ArangoDB arango = new ArangoDB.Builder()
                .host(host, port)
                .useSsl(ssl)
                .user(user)
                .password(password)
                .build();
        Events.info(LOG, "arangodb_client_created", "host", host, "port", port, "ssl", ssl, "user", user);

        try {
            // Source database (read-only)
            ArangoDatabase sourceDb = arango.db(ontologyDbName);

            // Drop and recreate target database
            if (arango.db(phenotypeDbName).exists()) {
                arango.db(phenotypeDbName).drop();
                Events.warn(LOG, "database_dropped", "database", phenotypeDbName);
            }
            if (!arango.createDatabase(phenotypeDbName)) {
                throw new IllegalStateException("Could not create database: " + phenotypeDbName);
            }
            Events.info(LOG, "database_created", "database", phenotypeDbName);
            ArangoDatabase targetDb = arango.db(phenotypeDbName);

            // Drop existing graph in target if present
            if (targetDb.graph(phenotypeGraphName).exists()) {
                targetDb.graph(phenotypeGraphName).drop();
                Events.warn(LOG, "graph_dropped", "database", phenotypeDbName, "graph", phenotypeGraphName);
            }

            new InducedSubgraphBuilder(sourceDb, targetDb).build(
                    ontologyGraphName,
                    "CS",
                    phenotypeGraphName,
                    MAX_DEPTH
            );
        } finally {
            arango.shutdown();
        }
    }

    /** A database or graph name from the environment. An empty one would drop and recreate the wrong thing. */
    private static String requireName(Map<String, String> env, String variable) {
        String value = env.get(variable);
        if (value == null || value.isBlank()) {
            Events.error(LOG, "arangodb_config_missing", "variable", variable);
            throw new IllegalStateException(variable + " is not set");
        }
        return value;
    }
}
