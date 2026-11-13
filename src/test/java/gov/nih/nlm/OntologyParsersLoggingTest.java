package gov.nih.nlm;

import ch.qos.logback.classic.Level;
import org.apache.jena.graph.NodeFactory;
import org.apache.jena.graph.Triple;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashSet;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events OntologyElementParser, OntologyTripleParser and OntologyTupleWriter log. */
class OntologyParsersLoggingTest {

    private static final Path testOboDir = Paths.get(System.getProperty("user.dir")).resolve("src/test/data/obo");

    @Test
    void elementParserLogsEachFileWithItsVersion() {
        try (LogCapture logs = new LogCapture()) {
            OntologyElementParser.parseOntologyElements(List.of(testOboDir.resolve("macrophage.owl")));

            LogCapture.Event e = logs.one("ontology_file_parsed");
            assertEquals("INFO", e.level());
            assertEquals("macrophage.owl", e.get("file"));
            assertTrue(e.get("version_iri").toString().contains("2024-09-26"));
            assertTrue((int) e.get("records_out") > 0);
            assertTrue(e.get("duration_ms") instanceof Long);
        }
    }

    @Test
    void noFilesIsAnErrorEventBeforeTheException() {
        try (LogCapture logs = new LogCapture()) {
            assertThrows(IllegalStateException.class,
                    () -> OntologyElementParser.requireFiles(List.of(), "/data/obo", ".*\\.owl"));

            LogCapture.Event e = logs.one("ontology_files_not_found");
            assertEquals("ERROR", e.level());
            assertEquals(".*\\.owl", e.get("pattern"));
            assertEquals("/data/obo", e.get("dir"));
        }
    }

    @Test
    void tripleParserCountsWhatItSawAndKept() {
        try (LogCapture logs = new LogCapture()) {
            List<Triple> triples = OntologyTripleParser.collectTriplesFromFile(testOboDir.resolve("macrophage.owl"), false);

            LogCapture.Event e = logs.one("triples_collected");
            assertEquals("macrophage.owl", e.get("file"));
            assertEquals(triples.size(), e.get("records_out"));
            assertEquals((int) e.get("records_in") - triples.size(), e.get("records_rejected"));
            assertTrue((int) e.get("records_in") >= triples.size());
        }
    }

    @Test
    void selectedNamespacesAreOnlyDebug() {
        try (LogCapture logs = new LogCapture()) {
            OntologyTripleParser.collectTriplesFromFile(testOboDir.resolve("macrophage.owl"), false);
            assertTrue(logs.named("triple_namespaces_selected").isEmpty());
        }
        try (LogCapture logs = new LogCapture(Level.DEBUG)) {
            OntologyTripleParser.collectTriplesFromFile(testOboDir.resolve("macrophage.owl"), false);
            assertEquals("DEBUG", logs.one("triple_namespaces_selected").level());
        }
    }

    @Test
    void uniqueTriplesCountFilesAndSkipsRo() {
        try (LogCapture logs = new LogCapture()) {
            HashSet<Triple> unique = OntologyTripleParser.collectUniqueTriples(
                    List.of(testOboDir.resolve("macrophage.owl"), testOboDir.resolve("ro.owl")), false);

            LogCapture.Event e = logs.one("unique_triples_collected");
            assertEquals(1, e.get("files"));
            assertEquals(unique.size(), e.get("records_out"));
            assertEquals(1, logs.named("triples_collected").size());
        }
    }

    @Test
    void importsReportIsOneEventPerFile() {
        try (LogCapture logs = new LogCapture()) {
            OntologyTripleParser.reportImports(List.of(testOboDir.resolve("macrophage.owl"), testOboDir.resolve("ro.owl")));

            LogCapture.Event e = logs.one("ontology_imports_reported");
            assertEquals("macrophage.owl", e.get("file"));
            assertTrue(e.fields().containsKey("root_ns"));
            assertTrue(e.get("class_namespaces") instanceof java.util.Collection);
        }
    }

    @Test
    void importsReportContinuesPastAFileWithNoOntologyResource(@TempDir Path dir) throws Exception {
        Path bare = dir.resolve("bare.owl");
        java.nio.file.Files.writeString(bare, "<?xml version=\"1.0\"?>\n"
                + "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"\n"
                + "         xmlns:owl=\"http://www.w3.org/2002/07/owl#\">\n"
                + "  <owl:Class rdf:about=\"http://purl.obolibrary.org/obo/CL_0000001\"/>\n</rdf:RDF>\n");
        try (LogCapture logs = new LogCapture()) {
            OntologyTripleParser.reportImports(List.of(bare, testOboDir.resolve("macrophage.owl")));

            LogCapture.Event failed = logs.one("ontology_imports_report_failed");
            assertEquals("ERROR", failed.level());
            assertEquals("bare.owl", failed.get("file"));
            assertEquals("macrophage.owl", logs.one("ontology_imports_reported").get("file"));
        }
    }

    @Test
    void elementParserCountsWhatItPassesOverByReason(@TempDir Path dir) throws Exception {
        Path owl = dir.resolve("odd.owl");
        java.nio.file.Files.writeString(owl, "<?xml version=\"1.0\"?>\n"
                + "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"\n"
                + "         xmlns:owl=\"http://www.w3.org/2002/07/owl#\"\n"
                + "         xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\">\n"
                + "  <owl:Class rdf:about=\"http://x.org/obo/CL_1\"><rdfs:label>one</rdfs:label></owl:Class>\n"
                + "  <owl:Class rdf:about=\"http://x.org/obo/CL_1\"><rdfs:label>again</rdfs:label></owl:Class>\n"
                + "  <owl:Class rdf:about=\"http://x.org/obo/valid_1\"/>\n"
                + "  <owl:Class rdf:about=\"http://x.org/obo/plain\"/>\n"
                + "</rdf:RDF>\n");
        try (LogCapture logs = new LogCapture()) {
            OntologyElementParser.parseOntologyElements(List.of(owl));

            LogCapture.Event e = logs.one("ontology_file_parsed");
            assertEquals(3, e.get("records_rejected"));
            assertEquals(java.util.Map.of("duplicate_label", 1, "non_ontology_id", 1, "no_underscore_in_term", 1),
                    e.get("rejected_by_reason"));
        }
    }

    @Test
    void tupleWriterLogsBlankNodeSkipsAndTheWrite(@TempDir Path dir) throws Exception {
        Triple named = Triple.create(NodeFactory.createURI("http://x/a"), NodeFactory.createURI("http://x/p"),
                NodeFactory.createURI("http://x/b"));
        Triple blank = Triple.create(NodeFactory.createBlankNode(), NodeFactory.createURI("http://x/p"),
                NodeFactory.createURI("http://x/b"));
        Path output = dir.resolve("out/t.json");
        try (LogCapture logs = new LogCapture()) {
            OntologyTupleWriter.writeTuples(List.of(named, blank), output);

            LogCapture.Event skipped = logs.one("triples_skipped");
            assertEquals("WARN", skipped.level());
            assertEquals(1, skipped.get("records_rejected"));
            assertEquals("blank_node", skipped.get("reason"));
            LogCapture.Event written = logs.one("tuples_written");
            assertEquals(output.toString(), written.get("output_path"));
            assertEquals(1, written.get("records_out"));
            assertEquals(1, written.get("records_rejected"));
        }
    }

    @Test
    void tupleWriterWithNoBlankNodesDoesNotWarn(@TempDir Path dir) throws Exception {
        Triple named = Triple.create(NodeFactory.createURI("http://x/a"), NodeFactory.createURI("http://x/p"),
                NodeFactory.createURI("http://x/b"));
        try (LogCapture logs = new LogCapture()) {
            OntologyTupleWriter.writeTuples(List.of(named), dir.resolve("t.json"));
            assertTrue(logs.named("triples_skipped").isEmpty());
            assertFalse(logs.named("tuples_written").isEmpty());
        }
    }
}
