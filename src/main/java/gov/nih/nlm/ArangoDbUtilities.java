package gov.nih.nlm;

import com.arangodb.*;
import com.arangodb.entity.EdgeDefinition;
import com.arangodb.model.EdgeCollectionRemoveOptions;
import com.arangodb.model.VertexCollectionRemoveOptions;

import com.arangodb.entity.CollectionEntity;
import com.arangodb.entity.CollectionType;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.InetAddress;
import java.net.UnknownHostException;
import java.util.*;

/**
 * Provides utilities for managing named ArangoDB databases, graphs, vertex
 * collections, and edge collections.
 *
 * @author Raymond LeClair
 */
public class ArangoDbUtilities {

    private static final Logger LOG = LoggerFactory.getLogger(ArangoDbUtilities.class);

    /**
     * An ArangoDB instance
     */
    public final ArangoDB arangoDB;

    /**
     * Build the ArangoDB instance specified in the system environment.
     */
    public ArangoDbUtilities() {
        this(System.getenv());
    }

    /**
     * Build the ArangoDB instance specified in the provided environment, and log where it connects, never the
     * password.
     *
     * @param env Environment map
     * @throws IllegalStateException If ARANGO_DB_HOST or ARANGO_DB_PORT is not set
     */
    public ArangoDbUtilities(Map<String, String> env) {
        String host = requireEnv(env, "ARANGO_DB_HOST");
        int port = Integer.parseInt(requireEnv(env, "ARANGO_DB_PORT"));
        boolean ssl = useSsl(env);
        arangoDB = new ArangoDB.Builder().host(host, port).useSsl(ssl).user(env.get("ARANGO_DB_USER")).password(env.get("ARANGO_DB_PASSWORD")).build();
        Events.info(LOG, "arangodb_client_created", "host", host, "port", port, "ssl", ssl,
                "user", env.get("ARANGO_DB_USER"));
    }

    /**
     * Get a required variable from the environment, logging which one is missing before failing.
     *
     * @param env  Environment map
     * @param name Variable name
     * @return The value
     * @throws IllegalStateException If the variable is unset or blank
     */
    private static String requireEnv(Map<String, String> env, String name) {
        String value = env.get(name);
        if (value == null || value.isBlank()) {
            Events.error(LOG, "arangodb_config_missing", "variable", name);
            throw new IllegalStateException(name + " is not set");
        }
        return value;
    }

    /**
     * Whether to connect over TLS. An explicit ARANGO_DB_SCHEME wins (the
     * pipeline always sets it; see python/src/flows/_common.py), and must be
     * http or https, so a typo cannot silently downgrade the connection.
     * Otherwise http for a loopback or unset ARANGO_DB_HOST, where the local
     * container serves plain HTTP, and https for any other host.
     *
     * @param env Environment map
     * @return True if the resolved scheme is https
     * @throws IllegalArgumentException If ARANGO_DB_SCHEME is neither http nor
     *                                  https
     */
    public static boolean useSsl(Map<String, String> env) {
        String scheme = env.get("ARANGO_DB_SCHEME");
        if (scheme != null && !scheme.isBlank()) {
            String resolved = scheme.strip();
            if ("https".equalsIgnoreCase(resolved)) {
                return true;
            }
            if ("http".equalsIgnoreCase(resolved)) {
                return false;
            }
            throw new IllegalArgumentException("ARANGO_DB_SCHEME must be http or https, not \"" + scheme + "\"");
        }
        String host = env.get("ARANGO_DB_HOST");
        return !(host == null || host.isBlank() || isLoopback(host.strip()));
    }

    /**
     * Whether a host names this machine, where ArangoDB serves plain HTTP by
     * default: localhost (any case) or a loopback IP literal. Only IP literals
     * are parsed, so a hostname never triggers a DNS lookup.
     *
     * @param host Host name or IP literal
     * @return True if the host is loopback
     */
    static boolean isLoopback(String host) {
        if ("localhost".equalsIgnoreCase(host)) {
            return true;
        }
        if (!host.contains(":") && !host.matches("[0-9.]+")) {
            return false;
        }
        try {
            return InetAddress.getByName(host).isLoopbackAddress();
        } catch (UnknownHostException e) {
            return false;
        }
    }

    /**
     * Exercise the utilities.
     *
     * @param args (None expected)
     */
    public static void main(String[] args) {
        ArangoDbUtilities arangoDbUtilities = new ArangoDbUtilities();
        String databaseName = "myDb";
        ArangoDatabase db = arangoDbUtilities.createOrGetDatabase(databaseName);
        String graphName = "myGraph";
        ArangoGraph graph = arangoDbUtilities.createOrGetGraph(db, graphName);
        String vertexOneName = "myVertexOne";
        arangoDbUtilities.createOrGetVertexCollection(graph, vertexOneName);
        String vertexTwoName = "myVertexTwo";
        arangoDbUtilities.createOrGetVertexCollection(graph, vertexTwoName);
        ArangoEdgeCollection edgeCollection = arangoDbUtilities.createOrGetEdgeCollection(graph, vertexOneName, vertexTwoName);
        arangoDbUtilities.deleteEdgeCollection(graph, edgeCollection.name());
        arangoDbUtilities.deleteVertexCollection(graph, vertexTwoName);
        arangoDbUtilities.deleteVertexCollection(graph, vertexOneName);
        arangoDbUtilities.deleteGraph(db, graphName);
        arangoDbUtilities.deleteDatabase(databaseName);
    }

    /**
     * Create or get a named database.
     *
     * @param databaseName Name of the database to create or get
     * @return Named database
     */
    public ArangoDatabase createOrGetDatabase(String databaseName) {
        // Create the database, if needed
        if (!arangoDB.db(databaseName).exists()) {
            if (!arangoDB.createDatabase(databaseName)) {
                throw new RuntimeException("Could not create database: " + databaseName);
            }
            Events.info(LOG, "database_created", "database", databaseName);
        }
        // Get the database
        return arangoDB.db(databaseName);
    }

    /**
     * Delete a named database.
     *
     * @param databaseName Name of the database to delete
     */
    public void deleteDatabase(String databaseName) {
        // Delete the database, if needed
        if (arangoDB.db(databaseName).exists()) {
            if (!arangoDB.db(databaseName).drop()) {
                throw new RuntimeException("Could not delete database: " + databaseName);
            }
            Events.warn(LOG, "database_dropped", "database", databaseName);
        }
    }

    /**
     * Create or get a named graph.
     *
     * @param db        Database in which to create or get the graph
     * @param graphName Name of the graph to create or get
     * @return Named graph
     */
    public ArangoGraph createOrGetGraph(ArangoDatabase db, String graphName) {
        // Create the graph, if needed
        if (!db.graph(graphName).exists()) {
            Collection<EdgeDefinition> edgeDefinitions = new ArrayList<>();
            db.createGraph(graphName, edgeDefinitions);
            Events.info(LOG, "graph_created", "database", db.name(), "graph", graphName);
        }
        // Get the graph
        return db.graph(graphName);
    }

    /**
     * Delete a named graph.
     *
     * @param db        Database in which to delete the graph
     * @param graphName Name of the graph to delete
     */
    public void deleteGraph(ArangoDatabase db, String graphName) {
        // Delete the graph, if needed
        if (db.graph(graphName).exists()) {
            db.graph(graphName).drop();
            Events.warn(LOG, "graph_dropped", "database", db.name(), "graph", graphName);
        }
    }

    /**
     * Create or get a named vertex collection.
     *
     * @param graph      Graph in which to create or get the vertex collection
     * @param vertexName Name of the vertex collection to create or get
     * @return Named vertex collection
     */
    public ArangoVertexCollection createOrGetVertexCollection(ArangoGraph graph, String vertexName) {
        // Create the vertex collection, if needed
        if (!graph.getVertexCollections().contains(vertexName)) {
            graph.addVertexCollection(vertexName);
            Events.debug(LOG, "vertex_collection_created", "graph", graph.name(), "collection", vertexName);
        }
        // Get the vertex collection
        return graph.vertexCollection(vertexName);
    }

    /**
     * Delete a named vertex collection.
     *
     * @param graph      Graph in which to delete the vertex collection
     * @param vertexName Name of the vertex collection to delete
     */
    public void deleteVertexCollection(ArangoGraph graph, String vertexName) {
        // Delete the vertex collection, if needed
        if (graph.getVertexCollections().contains(vertexName)) {
            VertexCollectionRemoveOptions options = new VertexCollectionRemoveOptions();
            options.dropCollection(true);
            graph.vertexCollection(vertexName).remove(options);
            Events.warn(LOG, "vertex_collection_deleted", "graph", graph.name(), "collection", vertexName);
        }
    }

    /**
     * Create, or get a named edge collection from and to the named vertices.
     *
     * @param graph          Graph in which to create, or get the edge collection
     * @param fromVertexName Name of the vertex collection from which the edge
     *                       originates
     * @param toVertexName   Name of the vertex collection to which the edge
     *                       terminates
     * @return Named edge collection
     */
    public ArangoEdgeCollection createOrGetEdgeCollection(ArangoGraph graph, String fromVertexName, String toVertexName) {
        // Create edge collection, if needed
        String collectionName = fromVertexName + "-" + toVertexName;
        if (!graph.getEdgeDefinitions().contains(collectionName)) {
            EdgeDefinition edgeDefinition = new EdgeDefinition().collection(collectionName).from(fromVertexName).to(toVertexName);
            graph.addEdgeDefinition(edgeDefinition);
            Events.debug(LOG, "edge_definition_created", "graph", graph.name(), "collection", collectionName);
        }
        // Get the edge collection
        return graph.edgeCollection(collectionName);
    }

    /**
     * Delete a named edge collection.
     *
     * @param graph    Graph in which to create, or get the edge collection
     * @param edgeName Name of the edge collection to delete
     */
    public void deleteEdgeCollection(ArangoGraph graph, String edgeName) {
        // Delete the edge collection, if needed
        if (graph.getEdgeDefinitions().contains(edgeName)) {
            EdgeCollectionRemoveOptions options = new EdgeCollectionRemoveOptions();
            options.dropCollections(true);
            graph.edgeCollection(edgeName).remove(options);
            Events.warn(LOG, "edge_definition_deleted", "graph", graph.name(), "collection", edgeName);
        }
    }

    /**
     * Log a summary of document counts for each vertex and edge
     * collection in the specified database.
     *
     * @param db Database to summarize
     * @return Map with "vertex" and "edge" keys, each mapping collection
     *         names to document counts (sorted alphabetically)
     */
    public Map<String, Map<String, Long>> printSummary(ArangoDatabase db) {
        Collection<CollectionEntity> collections = db.getCollections();
        List<CollectionEntity> vertexCollections = new ArrayList<>();
        List<CollectionEntity> edgeCollections = new ArrayList<>();
        for (CollectionEntity collection : collections) {
            if (collection.getIsSystem()) {
                continue;
            }
            if (collection.getType() == CollectionType.EDGES) {
                edgeCollections.add(collection);
            } else {
                vertexCollections.add(collection);
            }
        }
        vertexCollections.sort(Comparator.comparing(CollectionEntity::getName));
        edgeCollections.sort(Comparator.comparing(CollectionEntity::getName));

        Map<String, Long> vertexCounts = new LinkedHashMap<>();
        long vertexTotal = 0;
        for (CollectionEntity collection : vertexCollections) {
            long count = db.collection(collection.getName()).count().getCount();
            vertexTotal += count;
            vertexCounts.put(collection.getName(), count);
            Events.info(LOG, "collection_counted", "database", db.name(), "collection", collection.getName(),
                    "kind", "vertex", "records_out", count);
        }

        Map<String, Long> edgeCounts = new LinkedHashMap<>();
        long edgeTotal = 0;
        for (CollectionEntity collection : edgeCollections) {
            long count = db.collection(collection.getName()).count().getCount();
            edgeTotal += count;
            edgeCounts.put(collection.getName(), count);
            Events.info(LOG, "collection_counted", "database", db.name(), "collection", collection.getName(),
                    "kind", "edge", "records_out", count);
        }
        Events.info(LOG, "database_summary", "database", db.name(), "vertex_total", vertexTotal,
                "edge_total", edgeTotal);

        Map<String, Map<String, Long>> summary = new LinkedHashMap<>();
        summary.put("vertex", vertexCounts);
        summary.put("edge", edgeCounts);
        return summary;
    }
}
