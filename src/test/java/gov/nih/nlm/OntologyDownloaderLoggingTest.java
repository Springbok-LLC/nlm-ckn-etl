package gov.nih.nlm;

import ch.qos.logback.classic.Level;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events OntologyDownloader logs. */
class OntologyDownloaderLoggingTest {

    private static String owl(String version) {
        String info = version == null ? "" : "<owl:versionInfo>" + version + "</owl:versionInfo>";
        return "<?xml version=\"1.0\"?>\n"
                + "<rdf:RDF xmlns:owl=\"http://www.w3.org/2002/07/owl#\""
                + " xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\">\n"
                + "<owl:Ontology rdf:about=\"http://purl.obolibrary.org/obo/t.owl\">" + info + "</owl:Ontology>\n"
                + "</rdf:RDF>\n";
    }

    private static void write(Path path, String version) throws IOException {
        Files.writeString(path, owl(version), StandardCharsets.UTF_8);
    }

    @Test
    void firstDownloadIsLoggedWithItsVersionAndHttpFields(@TempDir Path dir) throws IOException {
        write(dir.resolve("t-new.owl"), "2024-02-01");
        try (LogCapture logs = new LogCapture()) {
            OntologyDownloader.installDownload(dir.resolve("t-new.owl"), dir, "t", ".owl",
                    "url", "http://x/t.owl", "status_code", 200, "bytes", 10, "duration_ms", 5L);

            LogCapture.Event e = logs.one("ontology_downloaded");
            assertEquals("INFO", e.level());
            assertEquals("t.owl", e.get("ontology"));
            assertEquals("2024-02-01", e.get("version_new"));
            assertEquals("http://x/t.owl", e.get("url"));
            assertEquals(200, e.get("status_code"));
            assertEquals(10, e.get("bytes"));
        }
    }

    @Test
    void replacementLogsBothVersionsAndTheArchive(@TempDir Path dir) throws IOException {
        write(dir.resolve("t.owl"), "2024-01-01");
        write(dir.resolve("t-new.owl"), "2024-02-01");
        try (LogCapture logs = new LogCapture()) {
            OntologyDownloader.installDownload(dir.resolve("t-new.owl"), dir, "t", ".owl");

            LogCapture.Event e = logs.one("ontology_updated");
            assertEquals("2024-01-01", e.get("version_prev"));
            assertEquals("2024-02-01", e.get("version_new"));
            assertEquals(dir.resolve(".archive/t-2024-01-01.owl").toString(), e.get("archived_to"));
        }
    }

    @Test
    void notNewerIsUnchanged(@TempDir Path dir) throws IOException {
        write(dir.resolve("t.owl"), "2024-02-01");
        write(dir.resolve("t-new.owl"), "2024-02-01");
        try (LogCapture logs = new LogCapture()) {
            OntologyDownloader.installDownload(dir.resolve("t-new.owl"), dir, "t", ".owl");

            LogCapture.Event e = logs.one("ontology_unchanged");
            assertEquals("2024-02-01", e.get("version_cur"));
            assertEquals("2024-02-01", e.get("version_new"));
            assertTrue(logs.named("ontology_updated").isEmpty());
        }
    }

    @Test
    void unreadableVersionWarnsAndLeavesOutTheNullField(@TempDir Path dir) throws IOException {
        write(dir.resolve("t.owl"), null);
        write(dir.resolve("t-new.owl"), "2024-02-01");
        try (LogCapture logs = new LogCapture()) {
            OntologyDownloader.installDownload(dir.resolve("t-new.owl"), dir, "t", ".owl");

            LogCapture.Event warning = logs.one("ontology_version_not_found");
            assertEquals("WARN", warning.level());
            assertEquals(dir.resolve("t.owl").toString(), warning.get("file"));
            LogCapture.Event updated = logs.one("ontology_updated");
            assertFalse(updated.fields().containsKey("version_prev"));
        }
    }

    @Test
    void parsingEachFileIsOnlyDebug(@TempDir Path dir) throws IOException {
        write(dir.resolve("t.owl"), "2024-02-01");
        try (LogCapture logs = new LogCapture()) {
            OntologyDownloader.findOboVersion(dir.resolve("t.owl"));
            assertTrue(logs.events().isEmpty());
        }
        try (LogCapture logs = new LogCapture(Level.DEBUG)) {
            OntologyDownloader.findOboVersion(dir.resolve("t.owl"));
            assertEquals("DEBUG", logs.one("ontology_file_parsing").level());
        }
    }

    @Test
    void failedUrlIsAnErrorAndTheRunCountsIt(@TempDir Path dir) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/good.owl", exchange -> {
            byte[] body = owl("2024-03-01").getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.createContext("/missing.owl", exchange -> {
            exchange.sendResponseHeaders(404, -1);
            exchange.close();
        });
        server.start();
        try (LogCapture logs = new LogCapture()) {
            String base = "http://127.0.0.1:" + server.getAddress().getPort();
            assertThrows(IOException.class, () -> OntologyDownloader.updateDownloads(
                    List.of(base + "/missing.owl", base + "/good.owl"), dir, HttpClient.newHttpClient()));

            LogCapture.Event failed = logs.one("ontology_download_failed");
            assertEquals("ERROR", failed.level());
            assertEquals("missing.owl", failed.get("ontology"));
            assertEquals("IOException", failed.get("error_type"));
            LogCapture.Event downloaded = logs.one("ontology_downloaded");
            assertEquals("good.owl", downloaded.get("ontology"));
            assertEquals(200, downloaded.get("status_code"));
            LogCapture.Event done = logs.one("ontologies_downloaded");
            assertEquals(2, done.get("records_in"));
            assertEquals(1, done.get("records_out"));
            assertEquals(1, done.get("records_rejected"));
        } finally {
            server.stop(0);
        }
    }
}
