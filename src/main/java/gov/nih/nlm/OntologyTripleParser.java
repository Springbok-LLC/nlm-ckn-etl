package gov.nih.nlm;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.apache.jena.graph.Triple;
import org.apache.jena.ontapi.OntModelFactory;
import org.apache.jena.ontapi.model.OntClass;
import org.apache.jena.ontapi.model.OntModel;
import org.apache.jena.ontapi.model.OntStatement;
import org.apache.jena.rdf.model.Property;
import org.apache.jena.rdf.model.RDFNode;
import org.apache.jena.rdf.model.Resource;
import org.apache.jena.rdf.model.Statement;
import org.apache.jena.riot.RDFDataMgr;
import org.apache.jena.vocabulary.OWL;
import org.apache.jena.vocabulary.RDF;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import static gov.nih.nlm.OntologyElementParser.parseOntologyElements;
import static gov.nih.nlm.PathUtilities.OBO_DIR;
import static gov.nih.nlm.PathUtilities.listFilesMatchingPattern;

/**
 * Parses each ontology file in the data/obo directory to collect unique triples.
 */
public class OntologyTripleParser {

    private static final Logger LOG = LoggerFactory.getLogger(OntologyTripleParser.class);

    // Assign selected predicate namespaces
    private static final List<String> PREDICATE_NAMESPACES = List.of("http://purl.obolibrary.org/obo/",
            "http://purl.org/dc/",
            "http://www.geneontology.org/formats/oboInOwl#",
            "http://www.w3.org/2000/01/rdf-schema#",
            "http://www.w3.org/2002/07/owl#");

    // Additional class namespaces to include per ontology file, beyond the root namespace.
    // Key: OWL filename, Value: set of namespace prefixes to include.
    private static final Map<String, Set<String>> EXTRA_NAMESPACES = Map.of("cl.owl",
            Set.of("http://purl.obolibrary.org/obo/PR"));

    // Assign selected predicates to exclude
    private static final List<String> EXCLUDED_PREDICATES = List.of(
            "http://purl.obolibrary.org/obo/mondo#curated_content_resource");

    /**
     * Build the set of namespaces for a given ontology file. Includes the root namespace plus any additional namespaces
     * configured in EXTRA_NAMESPACES.
     *
     * @param rootNS   Root namespace derived from the ontology
     * @param fileName Name of the OWL file
     * @return Set of namespace prefixes to accept
     */
    static Set<String> getNamespaces(String rootNS, String fileName) {
        Set<String> namespaces = new HashSet<>();
        namespaces.add(rootNS);
        Set<String> extras = EXTRA_NAMESPACES.get(fileName);
        if (extras != null) {
            namespaces.addAll(extras);
        }
        return namespaces;
    }

    /**
     * Test whether a URI starts with any of the given namespace prefixes.
     *
     * @param uri        URI to check
     * @param namespaces Set of namespace prefixes
     * @return true if the URI starts with any namespace
     */
    private static boolean matchesAnyNamespace(String uri, Set<String> namespaces) {
        for (String ns : namespaces) {
            if (uri.startsWith(ns)) return true;
        }
        return false;
    }

    /**
     * Test whether a triple is valid by checking that the subject matches one of the accepted namespaces, and
     * optionally that any named object also matches.
     *
     * @param triple             Triple to validate
     * @param namespaces         Set of namespace prefixes to accept
     * @param testObjectInRootNS If true, also validate that named objects match an accepted namespace
     * @return true if the triple is valid
     */
    public static boolean isValidTriple(Triple triple, Set<String> namespaces, boolean testObjectInRootNS) {
        boolean subjectIsValid = matchesAnyNamespace(triple.getSubject().toString(), namespaces);
        // triple.getPredicate().getURI().equals("http://purl.obolibrary.org/obo/mondo#curated_content_resource")
        boolean predicateIsValid = !EXCLUDED_PREDICATES.stream().anyMatch(ep -> triple.getPredicate().getURI().equals(ep));
        if (testObjectInRootNS) {
            boolean objectIsNamedResource = triple.getObject().isURI();
            boolean objectMatchesNS = matchesAnyNamespace(triple.getObject().toString(), namespaces);
            return subjectIsValid && predicateIsValid && (!objectIsNamedResource || objectMatchesNS);
        }
        return subjectIsValid && predicateIsValid;
    }

    /**
     * @deprecated Use {@link #isValidTriple(Triple, Set, boolean)} instead.
     */
    @Deprecated
    public static boolean isValidTriple(Triple triple, String rootNS, boolean testObjectInRootNS) {
        return isValidTriple(triple, Set.of(rootNS), testObjectInRootNS);
    }

    /**
     * Read an OWL file and identify the accepted namespaces. Collect triples from statements which contain a named
     * object and a predicate in one of the specified namespaces. Handle statements which contain an anonymous object
     * and an rdfs:subClassOf predicate by flattening all statements about the anonymous object into a single statement
     * with a named subject and object, then collecting the triple from the single statement. Optionally skip statements
     * with a named object not in an accepted namespace.
     *
     * @param owlFile            Path to OWL file
     * @param testObjectInRootNS Flag to check that named objects are in an accepted namespace
     * @return List of triples with named subject and object nodes
     */
    public static List<Triple> collectTriplesFromFile(Path owlFile, boolean testObjectInRootNS) {
        List<Triple> triples = new ArrayList<>();
        long startTime = System.nanoTime();
        int statementsSeen = 0;

        // Read the OWL file
        OntModel ontModel = OntModelFactory.createModel();
        RDFDataMgr.read(ontModel, owlFile.toString());

        // Build the set of accepted namespaces for this file
        String rootNS = getRootNS(ontModel);
        String fileName = owlFile.getFileName().toString();
        Set<String> namespaces = getNamespaces(rootNS, fileName);
        Events.debug(LOG, "triple_namespaces_selected", "file", fileName, "namespaces", new TreeSet<>(namespaces));

        // Consider each statement about each class in an accepted namespace
        for (OntClass ontClass : ontModel.classes().toList()) {
            if (!matchesAnyNamespace(ontClass.getURI(), namespaces)) {
                continue;
            }
            for (OntStatement classStatement : ontClass.statements().toList()) {
                statementsSeen++;
                String predicateURI = classStatement.getPredicate().getURI();
                if (!classStatement.getObject().isAnon()) {
                    // Handle statements which contain a named object
                    if (PREDICATE_NAMESPACES.stream().anyMatch(ns -> predicateURI.startsWith(ns))) {
                        // Collect statements as triples which contain a predicate in one of the
                        // selected name spaces
                        Triple triple = classStatement.asTriple();
                        if (isValidTriple(triple, namespaces, testObjectInRootNS)) {
                            triples.add(triple);
                        }
                    }
                } else if (predicateURI.equals("http://www.w3.org/2000/01/rdf-schema#subClassOf")) {
                    // Handle statements which contain an anonymous object and an rdfs:subClassOf
                    // predicate by considering each statement about the anonymous object in order
                    // to flatten these statements into a single statement with a named subject and
                    // object
                    Resource subject = classStatement.getSubject();
                    Property predicate = null;
                    RDFNode object = null;
                    for (OntStatement objectStatement : ontModel.statements(classStatement.getObject().asResource(),
                            null,
                            null).toList()) {
                        String predicateResource = objectStatement.getPredicate().getURI();
                        if (predicateResource.equals("http://www.w3.org/2002/07/owl#onProperty")) {
                            predicate = ontModel.getProperty(objectStatement.getObject().asResource().getURI());
                        } else if (predicateResource.equals("http://www.w3.org/2002/07/owl#someValuesFrom")) {
                            object = objectStatement.getObject();
                        }
                    }
                    // Create the single statement, and collect it as a triple
                    if (predicate != null && object != null) {
                        Triple triple = ontModel.createStatement(subject, predicate, object).asTriple();
                        if (isValidTriple(triple, namespaces, testObjectInRootNS)) {
                            triples.add(triple);
                        }
                    }
                }
            }
        }
        Events.info(LOG, "triples_collected", "file", fileName, "records_in", statementsSeen,
                "records_out", triples.size(), "records_rejected", statementsSeen - triples.size(),
                "duration_ms", RunLog.elapsedMs(startTime));
        return triples;
    }

    /**
     * Find the root namespace term in the ontology term, or from the first class in the ontology (only expected for
     * taxslim.owl).
     *
     * @param ontModel An ontology model created on reading an OWL file
     * @return The root namespace
     */
    static String getRootNS(OntModel ontModel) {
        // Identify the root namespace
        Resource ontology = ontModel.listResourcesWithProperty(RDF.type, OWL.Ontology).nextOptional().orElse(null);
        Statement rootTerm;
        String rootNS;
        if (ontology != null) {
            rootTerm = ontology.getProperty(ontModel.createProperty("http://purl.obolibrary.org/obo/IAO_0000700"));
            if (rootTerm != null) {
                rootNS = rootTerm.getResource().getURI().split("_")[0];
            } else {
                if (ontModel.classes().findFirst().isPresent()) {
                    rootNS = ontModel.classes().findFirst().get().getURI().split("_")[0];
                } else {
                    throw new RuntimeException("No root NS found: first class found is null");
                }
            }
        } else {
            throw new RuntimeException("No root NS found: no ontology resource found");
        }
        return rootNS;
    }

    /**
     * Extract owl:imports URIs from an ontology model and derive namespace prefixes from them. For example, an import
     * of "http://purl.obolibrary.org/obo/go.owl" yields prefix "http://purl.obolibrary.org/obo/GO".
     *
     * @param ontModel An ontology model created on reading an OWL file
     * @return Sorted set of namespace prefixes derived from owl:imports
     */
    static Set<String> getImportNamespaces(OntModel ontModel) {
        Set<String> namespaces = new TreeSet<>();
        Resource ontology = ontModel.listResourcesWithProperty(RDF.type, OWL.Ontology).nextOptional().orElse(null);
        if (ontology == null) {
            return namespaces;
        }
        for (Statement stmt : ontModel.listStatements(ontology, OWL.imports, (RDFNode) null).toList()) {
            String importURI = stmt.getObject().asResource().getURI();
            // Derive namespace prefix: strip ".owl" suffix and uppercase the local name
            // e.g. "http://purl.obolibrary.org/obo/go.owl" -> "http://purl.obolibrary.org/obo/GO"
            String stripped = importURI.replaceFirst("\\.owl$", "");
            int lastSlash = stripped.lastIndexOf('/');
            if (lastSlash >= 0 && lastSlash < stripped.length() - 1) {
                String base = stripped.substring(0, lastSlash + 1);
                String localName = stripped.substring(lastSlash + 1);
                namespaces.add(base + localName.toUpperCase());
            }
        }
        return namespaces;
    }

    /**
     * Collect all unique namespace prefixes from class URIs in the model. The prefix is derived by splitting on "_" and
     * taking the portion before the first underscore. For example, "http://purl.obolibrary.org/obo/CL_0000235" yields
     * "http://purl.obolibrary.org/obo/CL".
     *
     * @param ontModel An ontology model created on reading an OWL file
     * @return Sorted set of namespace prefixes found in class URIs
     */
    static Set<String> getClassNamespaces(OntModel ontModel) {
        Set<String> namespaces = new TreeSet<>();
        for (OntClass ontClass : ontModel.classes().toList()) {
            String uri = ontClass.getURI();
            if (uri != null && uri.contains("_")) {
                namespaces.add(uri.split("_")[0]);
            }
        }
        return namespaces;
    }

    /**
     * Report namespace prefixes for each OWL file. Prints the root namespace, any owl:imports, and all class namespace
     * prefixes found in the file. Use this to decide which namespaces to include per ontology.
     *
     * @param files Paths to ontology files
     */
    public static void reportImports(List<Path> files) {
        for (Path file : files) {
            if (file.getFileName().toString().equals("ro.owl")) continue;
            OntModel ontModel = OntModelFactory.createModel();
            RDFDataMgr.read(ontModel, file.toString());
            String rootNS = getRootNS(ontModel);
            Set<String> imports = getImportNamespaces(ontModel);
            try {
                Set<String> classNS = getClassNamespaces(ontModel);
                Events.info(LOG, "ontology_imports_reported", "file", file.getFileName().toString(),
                        "root_ns", rootNS, "imports", imports, "class_namespaces", classNS);
            } catch (Exception e) {
                Events.failure(LOG, "ontology_imports_report_failed", e, "file", file.getFileName().toString());
            }
        }
    }

    /**
     * Collect unique triples with named subject and object nodes.
     *
     * @param files              Paths to ontology files
     * @param testObjectInRootNS Flag to check that named objects are in the root namespace
     * @return Set of unique triples with named subject and object nodes
     */
    public static HashSet<Triple> collectUniqueTriples(List<Path> files, boolean testObjectInRootNS) {
        HashSet<Triple> uniqueTriplesSet = new HashSet<>();
        long startTime = System.nanoTime();
        int filesParsed = 0;
        int triplesCollected = 0;
        for (Path file : files) {
            if (file.getFileName().toString().equals("ro.owl")) continue;
            List<Triple> triples = collectTriplesFromFile(file, testObjectInRootNS);
            uniqueTriplesSet.addAll(triples);
            filesParsed++;
            triplesCollected += triples.size();
        }
        Events.info(LOG, "unique_triples_collected", "files", filesParsed, "records_in", triplesCollected,
                "records_out", uniqueTriplesSet.size(), "duration_ms", RunLog.elapsedMs(startTime));
        return uniqueTriplesSet;
    }

    /**
     * Parse each ontology file in the data/obo directory to collect unique triples. Pass "--report-imports" to print
     * owl:imports for each ontology file instead.
     *
     * @param args Optional: "--report-imports" to print import namespaces
     */
    public static void main(String[] args) {
        RunLog.run("OntologyTripleParser", () -> run(args),
                "obo_dir", OBO_DIR.toString(), "args", String.join(" ", args));
    }

    private static void run(String[] args) throws IOException {

        // List ontology files
        String oboPattern = ".*\\.owl";
        List<Path> oboFiles = listFilesMatchingPattern(OBO_DIR.toString(), oboPattern);
        if (oboFiles.isEmpty()) {
            Events.error(LOG, "ontology_files_not_found", "pattern", oboPattern, "dir", OBO_DIR.toString());
            throw new IllegalStateException("No OBO files found matching the pattern " + oboPattern);
        }

        // Report imports if requested
        if (args.length > 0 && "--report-imports".equals(args[0])) {
            reportImports(oboFiles);
            return;
        }

        // Map terms and labels
        String roPattern = "ro.owl";
        List<Path> roFile = listFilesMatchingPattern(OBO_DIR.toString(), roPattern);
        if (roFile.isEmpty()) {
            Events.error(LOG, "ontology_files_not_found", "pattern", roPattern, "dir", OBO_DIR.toString());
            throw new IllegalStateException("No RO file found");
        }
        Map<String, OntologyElementMap> ontologyElementMaps = parseOntologyElements(roFile);

        // Collect unique triples
        HashSet<Triple> uniqueTriplesSet = collectUniqueTriples(oboFiles, false);
    }
}
