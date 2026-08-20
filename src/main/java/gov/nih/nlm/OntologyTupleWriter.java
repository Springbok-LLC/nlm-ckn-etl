package gov.nih.nlm;

import org.apache.jena.graph.Node;
import org.apache.jena.graph.Triple;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;

import static gov.nih.nlm.OntologyTripleParser.collectUniqueTriples;
import static gov.nih.nlm.PathUtilities.OBO_DIR;
import static gov.nih.nlm.PathUtilities.USR_DIR;
import static gov.nih.nlm.PathUtilities.listFilesMatchingPattern;

/**
 * Writes triples collected from ontology OWL files as a tuples JSON file, using the same format the Python tuple
 * writers emit and {@link ResultsGraphBuilder#readJsonFile(String)} consumes.
 * <p>
 * The triples are collected with {@link OntologyTripleParser#collectUniqueTriples(List, boolean)}, the call
 * {@link OntologyGraphBuilder} makes, so the tuples written here are exactly those loaded into the ontology graph,
 * including anonymous {@code rdfs:subClassOf} restrictions flattened into named relations such as {@code part_of}.
 * <p>
 * The default output is written outside {@code data/tuples-<run>/} on purpose: {@link ResultsGraphBuilder} loads every
 * JSON file found there, and these ontology tuples are intended for review, not for loading.
 */
public class OntologyTupleWriter {

    // Assign the default OWL file pattern and output path
    private static final String DEFAULT_PATTERN = "uberon.*\\.owl";
    private static final String RUN_NAME = System.getenv("CKN_RUN") != null ? System.getenv("CKN_RUN") : "full";
    public static final Path DEFAULT_OUTPUT = USR_DIR.resolve("data/audit-" + RUN_NAME).resolve("uberon-tuples.json");

    /**
     * Convert a node to the string used to represent it in a tuple: the URI of a named node, or the lexical value of a
     * literal node.
     *
     * @param n Node to convert
     * @return String representing the node, or null if the node is neither named nor literal
     */
    static String nodeToString(Node n) {
        if (n.isURI()) {
            return n.getURI();
        }
        if (n.isLiteral()) {
            return n.getLiteralValue().toString();
        }
        return null;
    }

    /**
     * Write triples as a tuples JSON file. Triples containing a blank node are skipped, since they cannot be
     * represented as a tuple. The file is written compactly: an ontology such as UBERON contributes enough triples that
     * pretty-printing costs more than it helps.
     *
     * @param triples Triples to write
     * @param output  Path to the output JSON file
     * @throws IOException On write
     */
    public static void writeTuples(Collection<Triple> triples, Path output) throws IOException {
        JSONArray tuplesJsonArray = new JSONArray();
        int nSkipped = 0;
        for (Triple triple : triples) {
            String subject = nodeToString(triple.getSubject());
            String predicate = nodeToString(triple.getPredicate());
            String object = nodeToString(triple.getObject());
            if (subject == null || predicate == null || object == null) {
                nSkipped++;
                continue;
            }
            JSONArray tupleJsonArray = new JSONArray();
            tupleJsonArray.put(subject);
            tupleJsonArray.put(predicate);
            tupleJsonArray.put(object);
            tuplesJsonArray.put(tupleJsonArray);
        }
        JSONObject jsonObject = new JSONObject();
        jsonObject.put("tuples", tuplesJsonArray);

        Path parent = output.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8)) {
            writer.write(jsonObject.toString());
        }
        if (nSkipped > 0) {
            System.out.println("Skipped " + nSkipped + " triples containing a blank node");
        }
        System.out.println("Wrote " + tuplesJsonArray.length() + " tuples to " + output);
    }

    /**
     * Collect unique triples from each ontology file in the data/obo directory matching a pattern, then write them as a
     * tuples JSON file.
     *
     * @param args Optional: "--pattern" OWL file pattern (default "uberon.*\.owl"), "--output" output path (default
     *             data/audit-&lt;run&gt;/uberon-tuples.json)
     * @throws IOException On read or write
     */
    public static void main(String[] args) throws IOException {

        // Parse arguments
        String pattern = DEFAULT_PATTERN;
        Path output = DEFAULT_OUTPUT;
        for (int iArg = 0; iArg < args.length; iArg++) {
            switch (args[iArg]) {
                case "--pattern" -> {
                    if (iArg + 1 == args.length) throw new IllegalArgumentException("--pattern requires a value");
                    pattern = args[++iArg];
                }
                case "--output" -> {
                    if (iArg + 1 == args.length) throw new IllegalArgumentException("--output requires a value");
                    output = Paths.get(args[++iArg]);
                }
                default -> throw new IllegalArgumentException("Unexpected argument " + args[iArg]);
            }
        }

        // List the ontology files
        List<Path> oboFiles = listFilesMatchingPattern(OBO_DIR.toString(), pattern);
        if (oboFiles.isEmpty()) {
            throw new RuntimeException("No OBO files found matching the pattern " + pattern);
        }
        System.out.println("Collecting triples from " + oboFiles);

        // Collect unique triples, and write them as tuples
        HashSet<Triple> uniqueTriples = collectUniqueTriples(oboFiles, false);
        writeTuples(uniqueTriples, output);
    }
}
