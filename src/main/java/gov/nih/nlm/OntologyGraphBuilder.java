package gov.nih.nlm;

import com.arangodb.ArangoDatabase;
import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoGraph;
import com.arangodb.ArangoVertexCollection;
import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import org.apache.jena.graph.Node;
import org.apache.jena.graph.Triple;

import java.io.BufferedWriter;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static gov.nih.nlm.OntologyElementParser.createURI;
import static gov.nih.nlm.OntologyElementParser.parseOntologyElements;
import static gov.nih.nlm.OntologyTripleParser.collectUniqueTriples;
import static gov.nih.nlm.PathUtilities.OBO_DIR;
import static gov.nih.nlm.PathUtilities.listFilesMatchingPattern;

/**
 * Loads triples parsed from each ontology file in the data/obo directory into a local ArangoDB server instance.
 */
public class OntologyGraphBuilder {

    // Assign location of deprecated terms and edge labels files
    public static final Path DEPRECATED_TERMS_FILE = OBO_DIR.resolve("deprecated_terms.txt");
    public static final Path EDGE_LABELS_FILE = OBO_DIR.resolve("edge_labels.txt");

    // Assign vertices to include in the graph
    private static final ArrayList<String> VALID_VERTICES = new ArrayList<>(Arrays.asList("BGS",
            "BMC",
            "CHEBI",
            "CHEMBL",
            "CL",
            "CS",
            "CSD",
            "GO",
            "GS",
            "HP",
            "HsapDv",
            "MONDO",
            "NCBITaxon",
            "NCT",
            "Orphanet",
            "PATO",
            "PR",
            "PUB",
            "UBERON"));

    // RO taxon-constraint predicates (only_in_taxon, never_in_taxon, in_taxon). These relate a class to a taxon as
    // ontology metadata, not biology, and are excluded from the graph even when they reference a permitted taxon
    // (e.g. protein | ONLY_IN_TAXON | homo sapiens).
    private static final Set<String> TAXON_CONSTRAINT_PREDICATES = new HashSet<>(Arrays.asList("RO_0002160",
            "RO_0002161",
            "RO_0002162"));

    /**
     * Parse a URI to find an ontology term, ID, and number, and test if the ID is a valid vertex.
     *
     * @param n Node from which to create VTuple
     * @return VTuple created from node
     */
    public static VTuple createVTuple(Node n) {
        VTuple vtuple = new VTuple(null, null, null, false);
        if (!n.isURI()) return vtuple;
        URI uri;
        try {
            uri = createURI(n.getURI());
        } catch (RuntimeException e) {
            return vtuple;
        }
        String path = uri.getPath();
        if (path == null) return vtuple;
        Path fileName = Paths.get(path).getFileName();
        if (fileName == null) return vtuple;
        String term = fileName.toString();
        // Split on the first delimiter only: the id is the ontology prefix and the
        // number is the whole remaining local identifier, which may itself contain
        // underscores (e.g. a CellSetDataset keyed "CSD_<dvid>__<organ>").  A plain
        // split("_") would break such a term into more than two tokens and cause it
        // to be rejected as an invalid vertex, silently dropping every organ-keyed
        // CellSetDataset and its edges.  The id (not the token count) is what gates
        // validity, via VALID_VERTICES below.
        String[] tokens = null;
        if (term.contains("_")) {
            tokens = term.split("_", 2);
        } else if (term.contains(":")) {
            tokens = term.split(":", 2);
        }
        String id;
        String number;
        if (tokens != null && tokens.length == 2 && !tokens[1].isEmpty()) {
            id = tokens[0];
            number = tokens[1];
        } else {
            return vtuple;
        }
        // The graph is restricted to a fixed set of organisms, so only the permitted NCBITaxon nodes are valid
        // vertices; references to any other taxon (e.g. an Uberon class in_taxon an elephant) are left off the graph.
        boolean isValidVertex = VALID_VERTICES.contains(id)
                && !("NCBITaxon".equals(id) && !TaxonConfig.PERMITTED_TAXA.contains(number));
        return new VTuple(term, id, number, isValidVertex);
    }

    /**
     * Whether the given predicate term names a taxon-constraint relation (only_in_taxon, never_in_taxon, or in_taxon).
     * Such relations are ontology metadata, not biology, and are excluded from the graph even when they reference a
     * permitted taxon.
     *
     * @param predicateTerm The predicate term (e.g. {@code "RO_0002160"})
     * @return {@code true} if the predicate is a taxon-constraint relation
     */
    static boolean isTaxonConstraintPredicate(String predicateTerm) {
        return TAXON_CONSTRAINT_PREDICATES.contains(predicateTerm);
    }

    /**
     * Parse a URI node to obtain the fragment or last element of the path. Useful only for predicate nodes.
     *
     * @param ontologyElementMaps Maps terms and labels
     * @param p                   Predicate node to parse
     * @return Label resulting from parsing the node
     */
    public static PTuple parsePredicate(Map<String, OntologyElementMap> ontologyElementMaps,
                                        Node p) throws RuntimeException {
        String path;
        String fragment;
        String term;
        String curie;
        String label = null;
        if (p.isURI()) {
            path = createURI(p.getURI()).getPath();
            fragment = createURI(p.getURI()).getFragment();
            term = path.substring(path.lastIndexOf("/") + 1);
            if (fragment == null) {
                if (path.contains("/dc/")) {
                    curie = "dc:" + term;
                    label = term;
                } else {
                    curie = term.replace("_", ":");
                    if (ontologyElementMaps.get("ro").getTerms().containsKey(term)) {
                        label = ontologyElementMaps.get("ro").getTerms().get(term).label();
                    }
                }
            } else {
                curie = term.replace("rdf-schema", "rdfs") + ":" + fragment;
                label = fragment;
            }
        } else {
            throw new RuntimeException("Unexpected predicate " + p);
        }
        return new PTuple(term, curie, label);
    }

    /**
     * Store the first value for an attribute as a String; promote to an ArrayList on collision, deduplicating so the
     * same value is not stored twice.
     *
     * @param doc       Document on which to set the attribute
     * @param attribute Attribute name
     * @param value     Value to store or append
     */
    public static void addOrPromoteAttribute(BaseDocument doc, String attribute, String value) {
        Object existing = doc.getAttribute(attribute);
        if (existing == null) {
            doc.addAttribute(attribute, value);
        } else if (existing instanceof String s) {
            if (!s.equals(value)) {
                ArrayList<String> values = new ArrayList<>();
                values.add(s);
                values.add(value);
                doc.updateAttribute(attribute, values);
            }
        } else if (existing instanceof List<?> list) {
            @SuppressWarnings("unchecked") ArrayList<String> values = (ArrayList<String>) list;
            if (!values.contains(value)) {
                values.add(value);
            }
        }
    }

    /**
     * Set the {@code _search} attribute of a vertex document from its collection and key, replacing any existing value.
     * Uses {@code addAttribute}, which always sets the attribute; {@code updateAttribute} would silently do nothing for
     * a document that does not yet have it.
     *
     * @param doc        Vertex document, whose key is set
     * @param collection Vertex collection name
     */
    static void addSearchField(BaseDocument doc, String collection) {
        doc.addAttribute("_search", SearchFieldBuilder.build(collection, doc.getKey()));
    }

    /**
     * Construct vertices using triples parsed from specified ontology files that contain a named subject and object
     * which contain an ontology ID contained in the valid vertices' collection.
     *
     * @param uniqueTriples     Unique triples with which to construct vertices
     * @param arangoDbUtilities Utilities for accessing ArangoDB
     * @param graph             ArangoDB graph in which to create vertex collections
     * @param vertexCollections ArangoDB vertex collections
     * @param vertexDocuments   ArangoDB vertex documents
     */
    public static void constructVertices(HashSet<Triple> uniqueTriples,
                                         ArangoDbUtilities arangoDbUtilities,
                                         ArangoGraph graph,
                                         Map<String, ArangoVertexCollection> vertexCollections,
                                         Map<String, Map<String, BaseDocument>> vertexDocuments) {

        // Collect vertex keys for each vertex collection to prevent constructing
        // duplicate vertices in the vertex collection
        Map<String, Set<String>> vertexKeys = new HashMap<>();

        // Process triples
        long startTime = System.nanoTime();
        System.out.println("Constructing vertices using " + uniqueTriples.size() + " triples");
        int nVertices = 0;
        for (Triple triple : uniqueTriples) {

            // Consider the subject and object nodes
            ArrayList<Node> nodes = new ArrayList<>(Arrays.asList(triple.getSubject(), triple.getObject()));
            for (Node n : nodes) {

                // Construct a vertex from the current node, if it contains a valid id
                VTuple vtuple = createVTuple(n);
                if (vtuple.isValidVertex) {

                    // Create a vertex collection, if needed
                    if (!vertexCollections.containsKey(vtuple.id)) {
                        vertexCollections.put(vtuple.id,
                                arangoDbUtilities.createOrGetVertexCollection(graph, vtuple.id));
                        vertexDocuments.put(vtuple.id, new HashMap<>());
                        vertexKeys.put(vtuple.id, new HashSet<>());
                    }

                    // Construct the vertex, if needed
                    if (!vertexKeys.get(vtuple.id).contains(vtuple.number)) {
                        nVertices++;
                        BaseDocument doc = new BaseDocument(vtuple.number);
                        vertexDocuments.get(vtuple.id).put(vtuple.number, doc);
                        vertexKeys.get(vtuple.id).add(vtuple.number);
                    }
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Constructed " + nVertices + " vertices using " + uniqueTriples.size() + " triples in " + (stopTime - startTime) / 1e9 + " s");
    }

    /**
     * Update vertices using triples parsed from specified ontology files that contain a named subject which contains an
     * ontology ID contained in the valid vertices collection, and a filled object literal.
     *
     * @param uniqueTriples   Unique triples with which to update
     * @param vertexDocuments ArangoDB vertex documents
     */
    public static void updateVertices(HashSet<Triple> uniqueTriples,
                                      Map<String, OntologyElementMap> ontologyElementMaps,
                                      Map<String, Map<String, BaseDocument>> vertexDocuments) throws RuntimeException {

        // Process triples
        long startTime = System.nanoTime();
        System.out.println("Updating vertices using " + uniqueTriples.size() + " triples");
        Set<String> updatedVertices = new HashSet<>(); // For counting only
        for (Triple triple : uniqueTriples) {

            // Ensure the object contains a literal
            Node o = triple.getObject();
            if (!o.isLiteral()) {
                continue;
            }

            // Ensure the subject contains a valid ontology ID
            VTuple vtuple = createVTuple(triple.getSubject());
            if (vtuple.isValidVertex) {

                // Parse the predicate and object
                String attribute = parsePredicate(ontologyElementMaps, triple.getPredicate()).label();
                String literal = o.getLiteralValue().toString();
                if (attribute != null && literal != null) {
                    // Get the vertex to update
                    updatedVertices.add(vtuple.id + "-" + vtuple.number);
                    BaseDocument doc = vertexDocuments.get(vtuple.id).get(vtuple.number);
                    // Store the first value for an attribute as a String; promote to an ArrayList on collision
                    addOrPromoteAttribute(doc, attribute, literal);
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Updated " + updatedVertices.size() + " vertices using " + uniqueTriples.size() + " triples in " + (stopTime - startTime) / 1e9 + " s");
    }

    /**
     * Outcome of inserting documents, counting what actually happened rather than what was attempted.
     *
     * @param attempted Documents considered
     * @param inserted  Documents newly written
     * @param updated   Documents that already existed and were updated
     * @param skipped   Documents deliberately not written (deprecated vertices, edges with a missing endpoint)
     * @param failed    Documents whose insert or update raised an error
     */
    public record InsertSummary(int attempted, int inserted, int updated, int skipped, int failed) {
    }

    /**
     * Insert all vertices after they have been constructed and updated to improve performance.
     *
     * @param vertexCollections ArangoDB vertex collections
     * @param vertexDocuments   ArangoDB vertex documents
     * @return Counts of vertices inserted, updated, skipped, and failed
     * @throws IllegalStateException if any vertex could not be inserted or updated, after all have been attempted
     */
    public static InsertSummary insertVertices(Map<String, ArangoVertexCollection> vertexCollections,
                                               Map<String, Map<String, BaseDocument>> vertexDocuments) throws IOException {
        return insertVertices(vertexCollections, vertexDocuments, DEPRECATED_TERMS_FILE);
    }

    /**
     * As {@link #insertVertices(Map, Map)}, writing skipped deprecated terms to the given file.
     *
     * @param vertexCollections   ArangoDB vertex collections
     * @param vertexDocuments     ArangoDB vertex documents
     * @param deprecatedTermsFile File to which the identifiers of skipped deprecated terms are written
     * @return Counts of vertices inserted, updated, skipped, and failed
     * @throws IllegalStateException if any vertex could not be inserted or updated, after all have been attempted
     */
    static InsertSummary insertVertices(Map<String, ArangoVertexCollection> vertexCollections,
                                        Map<String, Map<String, BaseDocument>> vertexDocuments,
                                        Path deprecatedTermsFile) throws IOException {
        System.out.println("Inserting vertices");
        long startTime = System.nanoTime();
        int nVertices = 0;
        int nInserted = 0;
        int nUpdated = 0;
        int nSkipped = 0;
        int nFailed = 0;
        try (BufferedWriter deprecatedTermsWriter = Files.newBufferedWriter(deprecatedTermsFile,
                StandardCharsets.US_ASCII)) {
            for (String id : vertexDocuments.keySet()) {
                ArangoVertexCollection vertexCollection = vertexCollections.get(id);
                for (String number : vertexDocuments.get(id).keySet()) {
                    nVertices++;
                    BaseDocument doc = vertexDocuments.get(id).get(number);
                    if (vertexCollection.getVertex(doc.getKey(), doc.getClass()) == null) {
                        Object deprecated = doc.getAttribute("deprecated");
                        Object label = doc.getAttribute("label");
                        if ((deprecated != null && deprecated.toString().contains("true")) || (label != null && label.toString().contains(
                                "obsolete"))) {
                            deprecatedTermsWriter.write(id + "_" + number + "\n");
                            nSkipped++;
                            continue;
                        }
                        addSearchField(doc, id);
                        try {
                            vertexCollection.insertVertex(doc);
                            nInserted++;
                        } catch (Exception e) {
                            nFailed++;
                            System.err.println("Error inserting vertex " + doc + ": " + e.getMessage());
                        }
                    } else {
                        addSearchField(doc, id);
                        try {
                            vertexCollection.updateVertex(doc.getKey(), doc);
                            nUpdated++;
                        } catch (Exception e) {
                            nFailed++;
                            System.err.println("Error updating vertex " + doc + ": " + e.getMessage());
                        }
                    }
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Of " + nVertices + " vertices, inserted " + nInserted + ", updated " + nUpdated
                + ", skipped " + nSkipped + " deprecated, failed " + nFailed + " in " + (stopTime - startTime) / 1e9
                + " s");
        if (nFailed > 0) {
            throw new IllegalStateException(nFailed + " of " + nVertices + " vertices could not be inserted or updated");
        }
        return new InsertSummary(nVertices, nInserted, nUpdated, nSkipped, nFailed);
    }

    /**
     * Normalize edge sources by making all characters upper case.
     *
     * @param source Unnormalized source
     * @return Normalized source
     */
    public static String normalizeEdgeSource(String source) {
        return switch (source) {
            case "mondo-simple" -> "MONDO";
            case "taxslim" -> "NCBITAXON";
            case "go-plus" -> "GO";
            case "uberon-base" -> "UBERON";
            default -> source.toUpperCase();
        };
    }

    /**
     * Normalize edge labels by making all characters upper case, and replacing spaces with underscores. Handle special
     * cases.
     *
     * @param label Attribute value to normalize
     * @return Normalized attribute value
     */
    public static String normalizeEdgeLabel(String label) {
        return switch (label) {
            case "subClassOf" -> "SUB_CLASS_OF";
            case "disjointWith" -> "DISJOINT_WITH";
            case "crossSpeciesExactMatch" -> "CROSS_SPECIES_EXACT_MATCH";
            case "exactMatch" -> "EXACT_MATCH";
            case "equivalentClass" -> "EQUIVALENT_CLASS";
            case "seeAlso" -> "SEE_ALSO";
            // The ckn-schema described_in relation, currently mapped to the PROV placeholder PROV:describedIn
            case "describedIn" -> "DESCRIBED_IN";
            default -> label.toUpperCase().replace(" ", "_");
        };
    }

    /**
     * Construct edges using triples parsed from specified ontology files that contain a named subject and object which
     * contain an ontology ID contained in the valid vertices' collection.
     *
     * @param triples             Triples with which to construct edges
     * @param ontologyElementMaps Maps terms and labels
     * @param graph               ArangoDB graph in which to create vertex collections
     * @param edgeCollections     ArangoDB edge collections
     * @param edgeDocuments       ArangoDB edge documents
     */
    public static HashSet<String> constructEdges(HashSet<Triple> triples,
                                                 Map<String, OntologyElementMap> ontologyElementMaps,
                                                 ArangoDbUtilities arangoDbUtilities,
                                                 ArangoGraph graph,
                                                 Map<String, ArangoEdgeCollection> edgeCollections,
                                                 Map<String, Map<String, BaseEdgeDocument>> edgeDocuments) throws RuntimeException {

        // Collect edge keys in each edge collection to prevent constructing duplicate
        // edges in the edge collection
        Map<String, Set<String>> edgeKeys = new HashMap<>();

        // Process triples
        long startTime = System.nanoTime();
        System.out.println("Constructing edges using " + triples.size() + " triples");
        HashSet<String> edgeLabels = new HashSet<>();
        EdgeLabelFilter edgeLabelFilter = new EdgeLabelFilter();
        int nEdges = 0;
        for (Triple triple : triples) {

            // Ensure the subject contains a valid ontology ID
            VTuple subjectVTuple = createVTuple(triple.getSubject());
            if (!subjectVTuple.isValidVertex) continue;

            // Ensure the object contains a valid ontology ID
            VTuple objectVTuple = createVTuple(triple.getObject());
            if (!objectVTuple.isValidVertex) continue;

            // Parse the predicate and collect unique labels
            PTuple pTuple = parsePredicate(ontologyElementMaps, triple.getPredicate());
            if (pTuple.label() == null) {
                continue;
            }

            // Drop taxon-constraint relations; they are ontology metadata, not biology, and must not appear in the graph
            if (isTaxonConstraintPredicate(pTuple.term())) {
                continue;
            }

            // Drop edges between cell types and anatomical structures whose label was not reviewed and kept, and
            // leave their labels out of those collected
            String idPair = subjectVTuple.id + "-" + objectVTuple.id;
            if (!edgeLabelFilter.keep(idPair, normalizeEdgeLabel(pTuple.label()))) {
                continue;
            }
            edgeLabels.add(pTuple.label());

            // Create an edge collection, if needed
            if (!edgeCollections.containsKey(idPair)) {
                edgeCollections.put(idPair,
                        arangoDbUtilities.createOrGetEdgeCollection(graph, subjectVTuple.id, objectVTuple.id));
                edgeDocuments.put(idPair, new HashMap<>());
            }

            // Create an edge key set, if needed
            if (!edgeKeys.containsKey(idPair)) {
                edgeKeys.put(idPair, new HashSet<>());
            }

            // Construct the edge, if needed
            String key = subjectVTuple.number + "-" + pTuple.curie() + "-" + objectVTuple.number;
            BaseEdgeDocument doc;
            if (!edgeKeys.get(idPair).contains(key)) {
                nEdges++;
                doc = new BaseEdgeDocument(key,
                        subjectVTuple.id + "/" + subjectVTuple.number,
                        objectVTuple.id + "/" + objectVTuple.number);
                edgeDocuments.get(idPair).put(key, doc);
                edgeKeys.get(idPair).add(key);
            } else {
                doc = edgeDocuments.get(idPair).get(key);
            }

            // Store the first value for an attribute as a String; promote to an ArrayList on collision
            addOrPromoteAttribute(doc, "Label", normalizeEdgeLabel(pTuple.label()));
            addOrPromoteAttribute(doc, "Source", normalizeEdgeSource(subjectVTuple.id));
        }
        long stopTime = System.nanoTime();
        edgeLabelFilter.summarize().forEach(System.out::println);
        System.out.println("Constructed " + nEdges + " edges from " + triples.size() + " triples in " + (stopTime - startTime) / 1e9 + " s");
        return edgeLabels;
    }

    /**
     * Get the document collection name, which is typically an ontology id for a vertex document, or an ontology id pair
     * for an edge document, from a document id.
     *
     * @param documentId Document id
     * @return Document collection name
     */
    public static String getDocumentCollectionName(String documentId) {
        String documentCollectionName = null;
        if (documentId != null && documentId.contains("/")) {
            documentCollectionName = documentId.substring(0, documentId.indexOf("/"));
        }
        return documentCollectionName;
    }

    /**
     * Get the document key, which is typically an ontology term number for a vertex document, or an ontology term
     * number pair for an edge document, from a document id.
     *
     * @param documentId Document id
     * @return Document key
     */
    public static String getDocumentKey(String documentId) {
        String documentKey = null;
        if (documentId != null && documentId.contains("/")) {
            documentKey = documentId.substring(documentId.indexOf("/") + 1);
        }
        return documentKey;
    }

    /**
     * Insert all edges after they have been constructed to improve performance.
     *
     * @param vertexCollections ArangoDB vertex collections
     * @param edgeCollections   ArangoDB edge collections
     * @param edgeDocuments     ArangoDB edge documents
     * @return Counts of edges inserted, updated, skipped (an endpoint vertex is missing), and failed
     * @throws IllegalStateException if any edge could not be inserted or updated, after all have been attempted
     */
    public static InsertSummary insertEdges(Map<String, ArangoVertexCollection> vertexCollections,
                                            Map<String, ArangoEdgeCollection> edgeCollections,
                                            Map<String, Map<String, BaseEdgeDocument>> edgeDocuments) {
        System.out.println("Inserting edges");
        long startTime = System.nanoTime();
        int nEdges = 0;
        int nInserted = 0;
        int nUpdated = 0;
        int nSkipped = 0;
        int nFailed = 0;
        for (String idPair : edgeDocuments.keySet()) {
            ArangoEdgeCollection edgeCollection = edgeCollections.get(idPair);
            for (String key : edgeDocuments.get(idPair).keySet()) {
                nEdges++;
                BaseEdgeDocument doc = edgeDocuments.get(idPair).get(key);
                String docKey = doc.getKey();
                String fromId = doc.getFrom();
                String fromName = getDocumentCollectionName(fromId);
                String fromKey = getDocumentKey(fromId);
                String toId = doc.getTo();
                String toName = getDocumentCollectionName(toId);
                String toKey = getDocumentKey(toId);
                if (edgeCollection.getEdge(docKey, doc.getClass()) == null) {
                    if (!(vertexCollections.get(fromName).getVertex(fromKey,
                            BaseDocument.class) == null) && !(vertexCollections.get(toName).getVertex(toKey,
                            BaseDocument.class) == null)) {
                        try {
                            edgeCollection.insertEdge(doc);
                            nInserted++;
                        } catch (Exception e) {
                            nFailed++;
                            System.err.println("Error inserting edge " + doc + ": " + e.getMessage());
                        }
                    } else {
                        nSkipped++;
                    }
                } else {
                    try {
                        edgeCollection.updateEdge(docKey, doc);
                        nUpdated++;
                    } catch (Exception e) {
                        nFailed++;
                        System.err.println("Error updating edge " + doc + ": " + e.getMessage());
                    }
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Of " + nEdges + " edges, inserted " + nInserted + ", updated " + nUpdated
                + ", skipped " + nSkipped + " with a missing endpoint, failed " + nFailed + " in "
                + (stopTime - startTime) / 1e9 + " s");
        if (nFailed > 0) {
            throw new IllegalStateException(nFailed + " of " + nEdges + " edges could not be inserted or updated");
        }
        return new InsertSummary(nEdges, nInserted, nUpdated, nSkipped, nFailed);
    }

    /**
     * Load triples parsed from ontology files in the data/obo directory into a local ArangoDB server instance.
     *
     * @param args (None expected)
     */
    public static void main(String[] args) throws IOException {

        // List all ontology files
        String oboPath = OBO_DIR.toString();
        String oboPattern = ".*\\.owl";
        List<Path> oboFiles = listFilesMatchingPattern(oboPath, oboPattern);
        if (oboFiles.isEmpty()) {
            throw new RuntimeException("No OBO files found matching the pattern " + oboPattern);
        }

        // Parse ontology elements, and collect unique triples
        Map<String, OntologyElementMap> ontologyElementMaps = parseOntologyElements(oboFiles);
        HashSet<Triple> ontologyTriples = collectUniqueTriples(oboFiles, false);

        // Initialize the ontology database and graph
        String ontologyDatabaseName = "Cell-KN-Ontologies";
        String ontologyGraphName = "KN-Ontologies-v2.0";
        ArangoDbUtilities arangoDbUtilities = new ArangoDbUtilities();
        // Every insert/build step runs inside a try so a failure (e.g. an
        // IllegalStateException from insertVertices/insertEdges) still
        // disconnects from ArangoDB rather than leaking the connection.
        try {
            arangoDbUtilities.deleteDatabase(ontologyDatabaseName);
            ArangoDatabase ontologyDb = arangoDbUtilities.createOrGetDatabase(ontologyDatabaseName);
            arangoDbUtilities.deleteGraph(ontologyDb, ontologyGraphName);
            ArangoGraph ontologyGraph = arangoDbUtilities.createOrGetGraph(ontologyDb, ontologyGraphName);

            // Create, update, and insert the vertices
            Map<String, ArangoVertexCollection> ontologyVertexCollections = new HashMap<>();
            Map<String, Map<String, BaseDocument>> ontologyVertexDocuments = new HashMap<>();
            constructVertices(ontologyTriples,
                    arangoDbUtilities,
                    ontologyGraph,
                    ontologyVertexCollections,
                    ontologyVertexDocuments);
            updateVertices(ontologyTriples, ontologyElementMaps, ontologyVertexDocuments);
            insertVertices(ontologyVertexCollections, ontologyVertexDocuments);

            // Create, and insert the edges, capturing unique labels
            Map<String, ArangoEdgeCollection> ontologyEdgeCollections = new HashMap<>();
            Map<String, Map<String, BaseEdgeDocument>> ontologyEdgeDocuments = new HashMap<>();
            HashSet<String> edgeLabels = new HashSet<>(constructEdges(ontologyTriples,
                    ontologyElementMaps,
                    arangoDbUtilities,
                    ontologyGraph,
                    ontologyEdgeCollections,
                    ontologyEdgeDocuments));
            insertEdges(ontologyVertexCollections, ontologyEdgeCollections, ontologyEdgeDocuments);

            // Document unique labels, and their normalized values
            try (BufferedWriter edgeLabelsWriter = Files.newBufferedWriter(EDGE_LABELS_FILE, StandardCharsets.US_ASCII)) {
                for (String label : edgeLabels) {
                    edgeLabelsWriter.write(label + ": " + normalizeEdgeLabel(label) + "\n");
                }
            }

            // List the Cell Ontology file
            oboPattern = "cl.owl";
            oboFiles = listFilesMatchingPattern(oboPath, oboPattern);
            if (oboFiles.isEmpty()) {
                throw new RuntimeException("No CL files found matching the pattern " + oboPattern);
            }

            // Parse Cell Ontology elements, and collect unique triples
            Map<String, OntologyElementMap> phenotypeElementMaps = parseOntologyElements(oboFiles);
            phenotypeElementMaps.put("ro", ontologyElementMaps.get("ro"));
            HashSet<Triple> phenotypeTriples = collectUniqueTriples(oboFiles, false);

            // Initialize the phenotype database and subgraph
            String phenotypeDatabaseName = "Cell-KN-Phenotypes";
            String phenotypeGraphName = "KN-Phenotypes-v2.0";
            arangoDbUtilities.deleteDatabase(phenotypeDatabaseName);
            ArangoDatabase phenotypeDb = arangoDbUtilities.createOrGetDatabase(phenotypeDatabaseName);
            arangoDbUtilities.deleteGraph(phenotypeDb, phenotypeGraphName);
            ArangoGraph phenotypeGraph = arangoDbUtilities.createOrGetGraph(phenotypeDb, phenotypeGraphName);

            // Create, update, and insert the vertices
            Map<String, ArangoVertexCollection> phenotypeVertexCollections = new HashMap<>();
            Map<String, Map<String, BaseDocument>> phenotypeVertexDocuments = new HashMap<>();
            constructVertices(phenotypeTriples,
                    arangoDbUtilities,
                    phenotypeGraph,
                    phenotypeVertexCollections,
                    phenotypeVertexDocuments);
            updateVertices(phenotypeTriples, phenotypeElementMaps, phenotypeVertexDocuments);
            insertVertices(phenotypeVertexCollections, phenotypeVertexDocuments);

            // Create, and insert the edges, capturing unique labels
            Map<String, ArangoEdgeCollection> phenotypeEdgeCollections = new HashMap<>();
            Map<String, Map<String, BaseEdgeDocument>> phenotypeEdgeDocuments = new HashMap<>();
            edgeLabels.addAll(constructEdges(phenotypeTriples,
                    phenotypeElementMaps,
                    arangoDbUtilities,
                    phenotypeGraph,
                    phenotypeEdgeCollections,
                    phenotypeEdgeDocuments));
            insertEdges(phenotypeVertexCollections, phenotypeEdgeCollections, phenotypeEdgeDocuments);

        } finally {
            // Disconnect from a local ArangoDB server instance
            arangoDbUtilities.arangoDB.shutdown();
        }
    }

    // Define a record describing a vertex
    public record VTuple(String term, String id, String number, boolean isValidVertex) {

    }

    // Define a record describing a predicate
    public record PTuple(String term, String curie, String label) {
    }
}
