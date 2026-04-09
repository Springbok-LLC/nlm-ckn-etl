package gov.nih.nlm;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import javax.xml.stream.XMLStreamException;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OntologySlimmerTest {

    private static final Path testOboDir = Paths.get(System.getProperty("user.dir"),
            "src/test/data/obo");

    @Test
    void slimOntology_keepsHumanClasses(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        int count = OntologySlimmer.slimOntology(input, output, "9606");

        assertEquals(2, count);
    }

    @Test
    void slimOntology_outputContainsHumanClass(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        OntologySlimmer.slimOntology(input, output, "9606");

        String content = Files.readString(output);
        assertTrue(content.contains("PR_000000001"), "Should contain human class PR_000000001");
        assertTrue(content.contains("PR_000000004"), "Should contain human class PR_000000004");
    }

    @Test
    void slimOntology_excludesNonHumanClasses(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        OntologySlimmer.slimOntology(input, output, "9606");

        String content = Files.readString(output);
        assertFalse(content.contains("PR_000000002"), "Should not contain mouse class PR_000000002");
        assertFalse(content.contains("PR_000000003"), "Should not contain species-neutral class PR_000000003");
    }

    @Test
    void slimOntology_dropsAxioms(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        OntologySlimmer.slimOntology(input, output, "9606");

        String content = Files.readString(output);
        assertFalse(content.contains("owl:Axiom"), "Should not contain any owl:Axiom elements");
    }

    @Test
    void slimOntology_retainsHeader(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        OntologySlimmer.slimOntology(input, output, "9606");

        String content = Files.readString(output);
        assertTrue(content.contains("owl:Ontology"), "Should contain ontology header");
        assertTrue(content.contains("AnnotationProperty"), "Should contain annotation properties");
        assertTrue(content.contains("ObjectProperty"), "Should contain object properties");
    }

    @Test
    void slimOntology_outputIsValidXml(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        OntologySlimmer.slimOntology(input, output, "9606");

        String content = Files.readString(output);
        assertTrue(content.contains("<?xml"), "Should have XML declaration");
        assertTrue(content.contains("</rdf:RDF>"), "Should have closing rdf:RDF tag");
    }

    @Test
    void slimOntology_differentTaxon(@TempDir Path tempDir) throws IOException, XMLStreamException {
        Path input = testOboDir.resolve("pr-test.owl");
        Path output = tempDir.resolve("pr-slim.owl");

        int count = OntologySlimmer.slimOntology(input, output, "10090");

        assertEquals(1, count);
        String content = Files.readString(output);
        assertTrue(content.contains("PR_000000002"), "Should contain mouse class");
        assertFalse(content.contains("PR_000000001"), "Should not contain human class");
    }
}
