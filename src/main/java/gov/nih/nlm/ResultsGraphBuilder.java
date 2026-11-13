package gov.nih.nlm;

import com.arangodb.ArangoDatabase;
import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoGraph;
import com.arangodb.ArangoVertexCollection;
import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import gov.nih.nlm.OntologyGraphBuilder.PTuple;
import org.apache.jena.graph.Node;
import org.apache.jena.graph.NodeFactory;
import org.json.JSONArray;
import org.json.JSONObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

import static gov.nih.nlm.OntologyElementParser.parseOntologyElements;
import static gov.nih.nlm.OntologyGraphBuilder.createVTuple;
import static gov.nih.nlm.OntologyGraphBuilder.insertEdges;
import static gov.nih.nlm.OntologyGraphBuilder.insertVertices;
import static gov.nih.nlm.OntologyGraphBuilder.normalizeEdgeLabel;
import static gov.nih.nlm.OntologyGraphBuilder.parsePredicate;
import static gov.nih.nlm.PathUtilities.OBO_DIR;
import static gov.nih.nlm.PathUtilities.USR_DIR;
import static gov.nih.nlm.PathUtilities.listFilesMatchingPattern;

/**
 * Loads tuples parsed from results JSON files into a local ArangoDB server instance.
 */
public class ResultsGraphBuilder {

    private static final Logger LOG = LoggerFactory.getLogger(ResultsGraphBuilder.class);

    // Assign location of tuples and schema files
    private static final String RUN_NAME =
            System.getenv("CKN_RUN") != null ? System.getenv("CKN_RUN") : "full";
    public static final Path TUPLES_DIR = USR_DIR.resolve("data/tuples-" + RUN_NAME);

    // Lazily initialised so classes that only call pure static methods (e.g. readJsonFile)
    // can be loaded in tests without ArangoDB env vars being present.
    private static ArangoDbUtilities arangoDbUtilities;

    private static ArangoDbUtilities db() {
        if (arangoDbUtilities == null) {
            arangoDbUtilities = new ArangoDbUtilities();
        }
        return arangoDbUtilities;
    }

    // Assign triple indices
    private static final int TRIPLE_SUBJECT_IDX = 0;
    private static final int TRIPLE_PREDICATE_IDX = 1;
    private static final int TRIPLE_OBJECT_IDX = 2;

    // Assign quadruple indices
    private static final int QUINTUPLE_SUBJECT_IDX = 0;
    private static final int QUINTUPLE_PREDICATE_IDX = 1;
    private static final int QUINTUPLE_OBJECT_IDX = 2;
    private static final int QUINTUPLE_ATTRIBUTE_IDX = 3;
    private static final int QUINTUPLE_VALUE_IDX = 4;

    /**
     * Read a JSON file containing tuples, parsing each element as a URI or literal node, and validating the resulting
     * triples and quadruples.
     *
     * @param jsonFilePath Path to the JSON file containing tuples
     * @return List of tuples, each represented as a list of nodes
     * @throws IOException if the file cannot be read, contains invalid JSON, or contains invalid tuples
     */
    public static ArrayList<ArrayList<Node>> readJsonFile(String jsonFilePath) throws IOException {
        ArrayList<ArrayList<Node>> tuplesArrayList = new ArrayList<>();
        String content;
        try {
            content = new String(Files.readAllBytes(Paths.get(jsonFilePath)));
        } catch (IOException e) {
            throw new IOException("Error reading file: " + jsonFilePath, e);
        }
        JSONObject jsonObject;
        try {
            jsonObject = new JSONObject(content);
        } catch (org.json.JSONException e) {
            throw new IOException("Error parsing JSON in file: " + jsonFilePath, e);
        }
        JSONArray tuplesJsonArray = (JSONArray) jsonObject.get("tuples");
        for (int iTuple = 0; iTuple < tuplesJsonArray.length(); iTuple++) {
            ArrayList<Node> tupleArrayList = new ArrayList<>();
            JSONArray tupleJsonArray = (JSONArray) tuplesJsonArray.get(iTuple);
            for (int iElement = 0; iElement < tupleJsonArray.length(); iElement++) {
                String value = tupleJsonArray.get(iElement).toString();
                Node node;
                if (value.contains("http")) {
                    node = NodeFactory.createURI(value);
                } else {
                    node = NodeFactory.createLiteral(value);
                }
                tupleArrayList.add(node);
            }
            if (tupleArrayList.size() == 3 && !(tupleArrayList.get(TRIPLE_SUBJECT_IDX).isURI() && tupleArrayList.get(
                    TRIPLE_PREDICATE_IDX).isURI() && (tupleArrayList.get(TRIPLE_OBJECT_IDX).isURI() || tupleArrayList.get(
                    TRIPLE_OBJECT_IDX).isLiteral()))) {
                throw new IOException("Invalid triple " + tupleArrayList);
            }
            if (tupleArrayList.size() == 5 && !(tupleArrayList.get(QUINTUPLE_SUBJECT_IDX).isURI() && tupleArrayList.get(
                    QUINTUPLE_PREDICATE_IDX).isURI() && (tupleArrayList.get(QUINTUPLE_OBJECT_IDX).isURI() || tupleArrayList.get(
                    QUINTUPLE_OBJECT_IDX).isLiteral()) && (tupleArrayList.get(QUINTUPLE_ATTRIBUTE_IDX).isURI() || tupleArrayList.get(
                    QUINTUPLE_ATTRIBUTE_IDX).isLiteral()) && tupleArrayList.get(QUINTUPLE_VALUE_IDX).isLiteral())) {
                throw new IOException("Invalid triple " + tupleArrayList);
            }
            tuplesArrayList.add(tupleArrayList);
        }
        return tuplesArrayList;
    }

    /**
     * Construct vertices using tuples parsed from a results file that contain a filled subject and object which contain
     * an ontology ID contained in the valid vertices' collection.
     *
     * @param tuplesArrayList   list of tuples parsed from a results file
     * @param graph             ArangoDB graph in which to create vertex collections
     * @param vertexCollections ArangoDB vertex collections
     * @param vertexDocuments   ArangoDB vertex documents
     */
    public static void constructVertices(ArrayList<ArrayList<Node>> tuplesArrayList,
                                         ArangoGraph graph,
                                         Map<String, Set<String>> vertexKeys,
                                         Map<String, ArangoVertexCollection> vertexCollections,
                                         Map<String, Map<String, BaseDocument>> vertexDocuments) {

        int nVertices = 0;
        long startTime = System.nanoTime();
        for (ArrayList<Node> tupleArrayList : tuplesArrayList) {

            // Only construct vertices using triples
            if (tupleArrayList.size() != 3) continue;

            for (Node n : tupleArrayList) {

                // Only construct valid vertices
                OntologyGraphBuilder.VTuple vtuple = createVTuple(n);
                if (!vtuple.isValidVertex()) continue;

                // Create a vertex collection, if needed
                if (!vertexCollections.containsKey(vtuple.id())) {
                    vertexCollections.put(vtuple.id(),
                            db().createOrGetVertexCollection(graph, vtuple.id()));
                    vertexDocuments.put(vtuple.id(), new HashMap<>());
                    vertexKeys.put(vtuple.id(), new HashSet<>());
                }

                // Construct the vertex, if needed
                if (!vertexKeys.get(vtuple.id()).contains(vtuple.number())) {
                    nVertices++;
                    BaseDocument doc = new BaseDocument(vtuple.number());
                    vertexDocuments.get(vtuple.id()).put(vtuple.number(), doc);
                    vertexKeys.get(vtuple.id()).add(vtuple.number());
                }
            }
        }
        Events.debug(LOG, "vertices_constructed", "records_in", tuplesArrayList.size(), "records_out", nVertices,
                "duration_ms", RunLog.elapsedMs(startTime));
    }

    /**
     * Update vertices using tuples parsed from a results file that contain a filled subject which contains an ontology
     * ID contained in the valid vertices collection, and a filled object literal.
     *
     * @param tuplesArrayList     list of tuples parsed from a results file
     * @param ontologyElementMaps Maps terms and labels
     * @param vertexDocuments     ArangoDB vertex documents
     */
    public static void updateVertices(ArrayList<ArrayList<Node>> tuplesArrayList,
                                      Map<String, OntologyElementMap> ontologyElementMaps,
                                      Map<String, Map<String, BaseDocument>> vertexDocuments) throws RuntimeException {

        Set<String> updatedVertices = new HashSet<>(); // For counting only
        long startTime = System.nanoTime();
        for (ArrayList<Node> tupleArrayList : tuplesArrayList) {

            // Only update vertices using triples
            if (tupleArrayList.size() != 3) continue;

            // Ensure the object contains a literal
            Node o = tupleArrayList.get(TRIPLE_OBJECT_IDX);
            if (!o.isLiteral()) {
                continue;
            }

            // Parse the object
            String literal = o.getLiteralValue().toString();

            // Ensure the subject contains a valid ontology ID
            OntologyGraphBuilder.VTuple vtuple = createVTuple(tupleArrayList.get(TRIPLE_SUBJECT_IDX));
            if (!vtuple.isValidVertex()) continue;

            // Parse the predicate
            String attribute = parsePredicate(ontologyElementMaps, tupleArrayList.get(TRIPLE_PREDICATE_IDX)).label();

            // Update the corresponding vertex
            if (!vertexDocuments.get(vtuple.id()).containsKey(vtuple.number()))
                throw new RuntimeException("No vertex for VTuple " + vtuple);
            updatedVertices.add(vtuple.id() + "-" + vtuple.number());
            BaseDocument doc = vertexDocuments.get(vtuple.id()).get(vtuple.number());
            if (doc.getAttribute(attribute) == null) {
                doc.addAttribute(attribute, literal);
            } else {
                doc.updateAttribute(attribute, literal);
            }
        }
        Events.debug(LOG, "vertices_updated", "records_in", tuplesArrayList.size(), "records_out",
                updatedVertices.size(), "duration_ms", RunLog.elapsedMs(startTime));
    }

    /**
     * Construct edges using tuples parsed from a results file that contain a filled subject and object each which
     * contains an ontology ID contained in the valid vertices' collection.
     *
     * @param tuplesArrayList     list of tuples parsed from a results file
     * @param ontologyElementMaps Maps terms and labels
     * @param graph               ArangoDB graph
     * @param edgeKeys            ArangoDB edge keys for deduplication
     * @param edgeCollections     ArangoDB edge collections
     * @param edgeDocuments       ArangoDB edge documents
     * @param rejected            Count of tuples that made no edge, by reason, added to
     */
    public static void constructEdges(ArrayList<ArrayList<Node>> tuplesArrayList,
                                      Map<String, OntologyElementMap> ontologyElementMaps,
                                      ArangoGraph graph,
                                      Map<String, Set<String>> edgeKeys,
                                      Map<String, ArangoEdgeCollection> edgeCollections,
                                      Map<String, Map<String, BaseEdgeDocument>> edgeDocuments,
                                      Map<String, Integer> rejected) throws RuntimeException {

        int nEdges = 0;
        long startTime = System.nanoTime();
        for (ArrayList<Node> tupleArrayList : tuplesArrayList) {

            // Only construct edges using triples
            if (tupleArrayList.size() != 3) continue;

            // Ensure the subject contains a valid ontology ID
            OntologyGraphBuilder.VTuple subjectVTuple = createVTuple(tupleArrayList.get(TRIPLE_SUBJECT_IDX));
            if (!subjectVTuple.isValidVertex()) {
                // updateVertices skips an invalid subject too, so this is the one place it is counted, whatever
                // the object
                rejected.merge("invalid_vertex", 1, Integer::sum);
                continue;
            }

            // Ensure the object contains a valid ontology ID
            OntologyGraphBuilder.VTuple objectVTuple = createVTuple(tupleArrayList.get(TRIPLE_OBJECT_IDX));
            if (!objectVTuple.isValidVertex()) {
                // With a valid subject, a literal object is an attribute that updateVertices consumes
                if (tupleArrayList.get(TRIPLE_OBJECT_IDX).isURI()) {
                    rejected.merge("invalid_vertex", 1, Integer::sum);
                }
                continue;
            }

            // Parse the predicate
            PTuple pTuple = parsePredicate(ontologyElementMaps, tupleArrayList.get(TRIPLE_PREDICATE_IDX));
            if (pTuple.label() == null) {
                rejected.merge("no_label", 1, Integer::sum);
                continue;
            }

            // Create an edge collection, if needed
            String idPair = subjectVTuple.id() + "-" + objectVTuple.id();
            if (!edgeCollections.containsKey(idPair)) {
                edgeCollections.put(idPair,
                        db().createOrGetEdgeCollection(graph, subjectVTuple.id(), objectVTuple.id()));
                edgeDocuments.put(idPair, new HashMap<>());
                edgeKeys.put(idPair, new HashSet<>());
            }

            // Construct the edge, if needed
            String key = subjectVTuple.number() + "-" + pTuple.curie() + "-" + objectVTuple.number();
            BaseEdgeDocument doc;
            if (!edgeKeys.get(idPair).contains(key)) {
                nEdges++;
                doc = new BaseEdgeDocument(key,
                        subjectVTuple.id() + "/" + subjectVTuple.number(),
                        objectVTuple.id() + "/" + objectVTuple.number());
                edgeDocuments.get(idPair).put(key, doc);
                edgeKeys.get(idPair).add(key);
            } else {
                doc = edgeDocuments.get(idPair).get(key);
            }
            // Always assign the last label (add overwrites)
            doc.addAttribute("Label", normalizeEdgeLabel(pTuple.label()));
        }
        Events.debug(LOG, "edges_constructed", "records_in", tuplesArrayList.size(), "records_out", nEdges,
                "duration_ms", RunLog.elapsedMs(startTime));
    }

    /**
     * Update edges using tuples parsed from a results file that contain a filled subject and object each which contains
     * an ontology ID contained in the valid vertices collection, and two filled predicate literals.
     *
     * @param tuplesArrayList     list of tuples parsed from a results file
     * @param ontologyElementMaps Maps terms and labels
     * @param edgeDocuments       ArangoDB edge documents
     * @param rejected            Count of tuples that updated no edge, by reason, added to
     */
    public static void updateEdges(ArrayList<ArrayList<Node>> tuplesArrayList,
                                   Map<String, OntologyElementMap> ontologyElementMaps,
                                   Map<String, Map<String, BaseEdgeDocument>> edgeDocuments,
                                   Map<String, Integer> rejected) throws RuntimeException {

        Set<String> updatedEdges = new HashSet<>(); // For counting only
        long startTime = System.nanoTime();
        for (ArrayList<Node> tupleArrayList : tuplesArrayList) {

            // Only update edges using quintuples
            if (tupleArrayList.size() != 5) continue;

            // Ensure the subject contains a valid ontology ID
            OntologyGraphBuilder.VTuple subjectVTuple = createVTuple(tupleArrayList.get(QUINTUPLE_SUBJECT_IDX));
            if (!subjectVTuple.isValidVertex()) {
                rejected.merge("invalid_vertex", 1, Integer::sum);
                continue;
            }

            // Ensure the object contains a valid ontology ID
            OntologyGraphBuilder.VTuple objectVTuple = createVTuple(tupleArrayList.get(QUINTUPLE_OBJECT_IDX));
            if (!objectVTuple.isValidVertex()) {
                rejected.merge("invalid_vertex", 1, Integer::sum);
                continue;
            }

            // Parse the predicate
            PTuple pTuple = parsePredicate(ontologyElementMaps, tupleArrayList.get(QUINTUPLE_PREDICATE_IDX));

            // Parse the attribute
            String attribute = parsePredicate(ontologyElementMaps, tupleArrayList.get(QUINTUPLE_ATTRIBUTE_IDX)).label();

            // Parse the value
            String value = tupleArrayList.get(QUINTUPLE_VALUE_IDX).getLiteralValue().toString();

            // Update the corresponding edge
            String idPair = subjectVTuple.id() + "-" + objectVTuple.id();
            String key = subjectVTuple.number() + "-" + pTuple.curie() + "-" + objectVTuple.number();
            if (!edgeDocuments.get(idPair).containsKey(key))
                throw new RuntimeException("Invalid edge in collection " + idPair + " with key " + key);
            updatedEdges.add(subjectVTuple.id() + "/" + subjectVTuple.number() + "-" + objectVTuple.id() + "/" + objectVTuple.number());
            BaseEdgeDocument doc = edgeDocuments.get(idPair).get(key);
            if (doc.getAttribute(attribute) == null) {
                doc.addAttribute(attribute, value);
            } else {
                doc.updateAttribute(attribute, value);
            }
        }
        Events.debug(LOG, "edges_updated", "records_in", tuplesArrayList.size(), "records_out", updatedEdges.size(),
                "duration_ms", RunLog.elapsedMs(startTime));
    }

    /**
     * Load tuples parsed from a results file into a local ArangoDB server instance.
     *
     * @param args (None expected)
     */
    public static void main(String[] args) {
        RunLog.run("ResultsGraphBuilder", ResultsGraphBuilder::loadResults,
                "tuples_dir", TUPLES_DIR.toString(), "obo_dir", OBO_DIR.toString());
    }

    /** Loads every tuples file in the tuples directory into the ontology graph. */
    static void loadResults() throws IOException {

        // Identify the results tuples files
        String tuplesPath = TUPLES_DIR.toString();
        String tuplesPattern = ".*\\.json";
        List<Path> tuplesFiles = listFilesMatchingPattern(tuplesPath, tuplesPattern);
        if (tuplesFiles.isEmpty()) {
            Events.error(LOG, "tuples_files_not_found", "pattern", tuplesPattern, "dir", tuplesPath);
            throw new IllegalStateException("No tuples files found matching pattern " + tuplesPattern);
        }
        Events.info(LOG, "tuples_files_listed", "dir", tuplesPath, "files", tuplesFiles.size());

        // Map terms and labels
        String oboPath = OBO_DIR.toString();
        String oboPattern = "ro.owl";
        List<Path> oboFiles = listFilesMatchingPattern(oboPath, oboPattern);
        if (oboFiles.isEmpty()) {
            Events.error(LOG, "ontology_files_not_found", "pattern", oboPattern, "dir", oboPath);
            throw new IllegalStateException("No OBO files found matching the pattern " + oboPattern);
        }
        Map<String, OntologyElementMap> ontologyElementMaps = parseOntologyElements(oboFiles);

        // Every insert/build step runs inside a try so a failure (e.g. an
        // IllegalStateException from insertVertices/insertEdges) still
        // disconnects from ArangoDB rather than leaking the connection.
        try {
            // Create the database and graph
            String ontologyDatabaseName = "Cell-KN-Ontologies";
            ArangoDatabase ontologyDb = db().createOrGetDatabase(ontologyDatabaseName);
            String ontologyGraphName = "KN-Ontologies-v2.0";
            MDC.put("graph", ontologyGraphName);
            ArangoGraph ontologyGraph = db().createOrGetGraph(ontologyDb, ontologyGraphName);

            // Collect vertex keys for each vertex collection to prevent constructing
            // duplicate vertices in the vertex collection
            Map<String, Set<String>> ontologyVertexKeys = new HashMap<>();

            // Collect edge keys in each edge collection to prevent constructing duplicate
            // edges in the edge collection
            Map<String, Set<String>> ontologyEdgeKeys = new HashMap<>();

            // Collect all vertices and edges before inserting them into the graph for improved performance
            Map<String, ArangoVertexCollection> ontologyVertexCollections = new HashMap<>();
            Map<String, Map<String, BaseDocument>> ontologyVertexDocuments = new HashMap<>();
            Map<String, ArangoEdgeCollection> ontologyEdgeCollections = new HashMap<>();
            Map<String, Map<String, BaseEdgeDocument>> ontologyEdgeDocuments = new HashMap<>();

            // Read the results tuples files
            for (Path tuplesFile : tuplesFiles) {
                long fileStarted = System.nanoTime();
                try {
                    ArrayList<ArrayList<Node>> tuplesArrayList = readJsonFile(tuplesFile.toString());
                    Map<String, Integer> rejected = new TreeMap<>();

                    // Construct, and update vertices
                    constructVertices(tuplesArrayList,
                            ontologyGraph,
                            ontologyVertexKeys,
                            ontologyVertexCollections,
                            ontologyVertexDocuments);
                    updateVertices(tuplesArrayList, ontologyElementMaps, ontologyVertexDocuments);

                    // Construct, and update edges
                    constructEdges(tuplesArrayList,
                            ontologyElementMaps,
                            ontologyGraph,
                            ontologyEdgeKeys,
                            ontologyEdgeCollections,
                            ontologyEdgeDocuments,
                            rejected);
                    updateEdges(tuplesArrayList, ontologyElementMaps, ontologyEdgeDocuments, rejected);
                    logTuplesFile(tuplesFile, tuplesArrayList, rejected, fileStarted);
                } catch (IOException | RuntimeException e) {
                    Events.failure(LOG, "tuples_file_failed", e, "tuples_file", tuplesFile.toString(),
                            "duration_ms", RunLog.elapsedMs(fileStarted));
                    throw e;
                }
            }
            // Insert vertices, and edges
            insertVertices(ontologyVertexCollections, ontologyVertexDocuments);
            insertEdges(ontologyVertexCollections, ontologyEdgeCollections, ontologyEdgeDocuments);

        } finally {
            MDC.remove("graph");
            // Disconnect from a local ArangoDB server instance
            db().arangoDB.shutdown();
        }
    }

    /**
     * Log one {@code tuples_file_processed} event for a tuples file: how many tuples it held, how many made nothing,
     * and why. A tuple with other than 3 or 5 elements is rejected as {@code not_a_triple}; the build steps ignore it.
     */
    static void logTuplesFile(Path tuplesFile, ArrayList<ArrayList<Node>> tuples,
                                      Map<String, Integer> rejected, long startedNanos) {
        long triples = tuples.stream().filter(t -> t.size() == 3).count();
        long quintuples = tuples.stream().filter(t -> t.size() == 5).count();
        long neither = tuples.size() - triples - quintuples;
        if (neither > 0) {
            rejected.merge("not_a_triple", (int) neither, Integer::sum);
        }
        Events.info(LOG, "tuples_file_processed", "tuples_file", tuplesFile.toString(), "records_in", tuples.size(),
                "triples", triples, "quintuples", quintuples,
                "records_rejected", rejected.values().stream().mapToInt(Integer::intValue).sum(),
                "rejected_by_reason", rejected.isEmpty() ? null : rejected,
                "duration_ms", RunLog.elapsedMs(startedNanos));
    }
}
