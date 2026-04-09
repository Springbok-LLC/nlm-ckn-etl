package gov.nih.nlm;

import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OntologyDownloaderTest {

    private static final Path testOboDir = Paths.get(System.getProperty("user.dir")).resolve("src/test/data/obo");

    // --- findOboVersion tests ---

    @Test
    void findOboVersion_fromVersionInfo() {
        // version-info-test.owl has owl:versionInfo with text "2024-01-15"
        String version = OntologyDownloader.findOboVersion(testOboDir.resolve("version-info-test.owl"));
        assertEquals("2024-01-15", version);
    }

    @Test
    void findOboVersion_fromVersionIRI() {
        // macrophage.owl has owl:versionIRI but no owl:versionInfo
        String version = OntologyDownloader.findOboVersion(testOboDir.resolve("macrophage.owl"));
        assertEquals("2024-09-26", version);
    }

    @Test
    void findOboVersion_prefersVersionInfo() {
        // ro.owl has both owl:versionInfo and owl:versionIRI with the same date
        String version = OntologyDownloader.findOboVersion(testOboDir.resolve("ro.owl"));
        assertEquals("2024-04-24", version);
    }

    @Test
    void findOboVersion_noVersion() {
        // no-version-test.owl has neither owl:versionInfo nor owl:versionIRI
        String version = OntologyDownloader.findOboVersion(testOboDir.resolve("no-version-test.owl"));
        assertNull(version);
    }

    // --- OBO_PURLS tests ---

    @Test
    void oboPurls_containsExpectedUrls() {
        assertEquals(10, OntologyDownloader.OBO_PURLS.size());
        assertTrue(OntologyDownloader.OBO_PURLS.contains("http://purl.obolibrary.org/obo/cl.owl"));
        assertTrue(OntologyDownloader.OBO_PURLS.contains("http://purl.obolibrary.org/obo/ro.owl"));
    }

    // --- updateDownloads integration test ---

    @Tag("integration")
    @Test
    void updateDownloads_downloadAndCompareVersions() throws IOException, InterruptedException {
        Path tempDir = Files.createTempDirectory("obo-download-test");
        try {
            // Download a small OBO file (ro.owl)
            List<String> urls = List.of("http://purl.obolibrary.org/obo/ro.owl");
            OntologyDownloader.updateDownloads(urls, tempDir);

            // Verify the file was downloaded and renamed from ro-new.owl to ro.owl
            Path downloadedFile = tempDir.resolve("ro.owl");
            assertTrue(Files.exists(downloadedFile), "ro.owl should exist after first download");
            assertTrue(Files.size(downloadedFile) > 0, "ro.owl should not be empty");

            // Verify version can be extracted
            String version = OntologyDownloader.findOboVersion(downloadedFile);
            assertNotNull(version, "Downloaded ro.owl should have a parseable version");
            assertTrue(version.matches("\\d{4}-\\d{2}-\\d{2}"), "Version should be YYYY-MM-DD format");

            // Download again — should detect same version and remove the new file
            OntologyDownloader.updateDownloads(urls, tempDir);
            assertTrue(Files.exists(downloadedFile), "ro.owl should still exist after second download");
            Path newFile = tempDir.resolve("ro-new.owl");
            assertTrue(!Files.exists(newFile), "ro-new.owl should be removed (same version)");
        } finally {
            // Clean up temp directory
            try (var stream = Files.walk(tempDir)) {
                stream.sorted(java.util.Comparator.reverseOrder())
                        .map(Path::toFile)
                        .forEach(java.io.File::delete);
            }
        }
    }
}
