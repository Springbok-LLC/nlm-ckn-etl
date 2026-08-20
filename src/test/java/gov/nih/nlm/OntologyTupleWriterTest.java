package gov.nih.nlm;

import org.apache.jena.graph.Node;
import org.apache.jena.graph.Triple;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OntologyTupleWriterTest {

    private static final Path testOboDir = Paths.get(System.getProperty("user.dir")).resolve("src/test/data/obo");

    private static final String LABEL = "http://www.w3.org/2000/01/rdf-schema#label";
    private static final String ONLY_IN_TAXON = "http://purl.obolibrary.org/obo/RO_0002160";

    /**
     * Write the triples collected from the UBERON test ontology, then read them back with the reader
     * ResultsGraphBuilder uses.
     */
    private static ArrayList<ArrayList<Node>> writeAndReadUberonTuples(Path tempDir) throws IOException {
        List<Triple> triples = OntologyTripleParser.collectTriplesFromFile(testOboDir.resolve("uberon-test.owl"),
                false);
        Path output = tempDir.resolve("uberon-tuples.json");
        OntologyTupleWriter.writeTuples(triples, output);
        assertTrue(output.toFile().exists());
        return ResultsGraphBuilder.readJsonFile(output.toString());
    }

    @Test
    void writeTuples_roundTripsThroughResultsGraphBuilder(@TempDir Path tempDir) throws IOException {
        ArrayList<ArrayList<Node>> tuples = writeAndReadUberonTuples(tempDir);

        assertFalse(tuples.isEmpty());
        for (ArrayList<Node> tuple : tuples) {
            assertEquals(3, tuple.size());
            assertTrue(tuple.get(0).isURI(), "Subject should be a URI node: " + tuple.get(0));
            assertTrue(tuple.get(1).isURI(), "Predicate should be a URI node: " + tuple.get(1));
        }
    }

    @Test
    void writeTuples_containsLabelTuple(@TempDir Path tempDir) throws IOException {
        ArrayList<ArrayList<Node>> tuples = writeAndReadUberonTuples(tempDir);

        assertTrue(tuples.stream().anyMatch(t -> t.get(0).getURI().endsWith("UBERON_0000004") && t.get(1).getURI().equals(
                        LABEL) && t.get(2).isLiteral() && t.get(2).getLiteralValue().toString().equals("heart")),
                "Expected the heart label tuple");
    }

    @Test
    void writeTuples_containsFlattenedRestrictionTuple(@TempDir Path tempDir) throws IOException {
        ArrayList<ArrayList<Node>> tuples = writeAndReadUberonTuples(tempDir);

        // The insect wing class carries an anonymous only_in_taxon restriction, which the parser flattens into a
        // triple with a named object
        assertTrue(tuples.stream().anyMatch(t -> t.get(0).getURI().endsWith("UBERON_0000003") && t.get(1).getURI().equals(
                        ONLY_IN_TAXON) && t.get(2).isURI() && t.get(2).getURI().endsWith("NCBITaxon_7147")),
                "Expected the flattened only_in_taxon tuple");
    }

    @Test
    void nodeToString_returnsNullForBlankNode() {
        assertEquals(null, OntologyTupleWriter.nodeToString(org.apache.jena.graph.NodeFactory.createBlankNode()));
    }
}
