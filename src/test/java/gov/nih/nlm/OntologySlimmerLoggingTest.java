package gov.nih.nlm;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events OntologySlimmer logs. */
class OntologySlimmerLoggingTest {

    private static final Path testOboDir = Paths.get(System.getProperty("user.dir"), "src/test/data/obo");

    @Test
    void slimIsOneEventWithCountsAndMode(@TempDir Path dir) throws Exception {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = dir.resolve("pr-slim.owl");
        try (LogCapture logs = new LogCapture()) {
            int written = OntologySlimmer.slimOntologyByTaxonInclusion(input, output, Set.of("9606"));

            LogCapture.Event e = logs.one("ontology_slimmed");
            assertEquals("INFO", e.level());
            assertEquals("inclusion", e.get("mode"));
            assertEquals(input.toString(), e.get("input_file"));
            assertEquals(output.toString(), e.get("output_file"));
            assertEquals(written, e.get("records_out"));
            assertTrue((int) e.get("records_rejected") > 0);
            assertTrue(e.get("duration_ms") instanceof Long);
            assertTrue(logs.named("ontology_slim_empty").isEmpty());
        }
    }

    @Test
    void slimmingToNothingWarns(@TempDir Path dir) throws Exception {
        try (LogCapture logs = new LogCapture()) {
            OntologySlimmer.slimOntologyByTaxonInclusion(
                    testOboDir.resolve("pr-test.owl"), dir.resolve("out.owl"), Set.of("no-such-taxon"));

            assertEquals(0, logs.one("ontology_slimmed").get("records_out"));
            assertEquals("WARN", logs.one("ontology_slim_empty").level());
        }
    }
}
