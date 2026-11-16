package gov.nih.nlm;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.w3c.dom.Document;
import org.w3c.dom.Element;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpClient.Redirect;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static gov.nih.nlm.PathUtilities.OBO_DIR;

/**
 * Downloads ontology files from the OBO Foundry, comparing versions to manage updates.
 */
public class OntologyDownloader {

    private static final Logger LOG = LoggerFactory.getLogger(OntologyDownloader.class);

    // Assign OBO Foundry PURLs
    static final List<String> OBO_PURLS = List.of("https://purl.obolibrary.org/obo/cl.owl",
            "https://purl.obolibrary.org/obo/go.owl",
            "https://purl.obolibrary.org/obo/pr.owl",
            "https://purl.obolibrary.org/obo/uberon/uberon-base.owl",
            "https://purl.obolibrary.org/obo/ncbitaxon/subsets/taxslim.owl",
            "https://purl.obolibrary.org/obo/mondo/mondo-simple.owl",
            "https://purl.obolibrary.org/obo/hp.owl",
            "https://purl.obolibrary.org/obo/pato.owl",
            "https://purl.obolibrary.org/obo/hsapdv.owl",
            "https://purl.obolibrary.org/obo/ro.owl");
    // Assign pattern for extracting YYYY-MM-DD dates
    private static final Pattern DATE_PATTERN = Pattern.compile("(\\d{4}-\\d{2}-\\d{2})");

    /**
     * Parse the ontology XML file to find its version as a YYYY-MM-DD date string. First tries owl:versionInfo, then
     * falls back to extracting a date from owl:versionIRI.
     *
     * @param oboFilePath Path to ontology XML file
     * @return Version string in YYYY-MM-DD format, or null if not found
     */
    public static String findOboVersion(Path oboFilePath) {
        Events.debug(LOG, "ontology_file_parsing", "file", oboFilePath.toString());
        Document doc = OntologyElementParser.parseXmlFile(oboFilePath.toFile());

        // Try owl:versionInfo first
        Element versionInfoElement = (Element) doc.getElementsByTagName("owl:versionInfo").item(0);
        if (versionInfoElement != null) {
            String text = versionInfoElement.getTextContent().trim();
            Matcher matcher = DATE_PATTERN.matcher(text);
            if (matcher.find()) {
                return matcher.group(1);
            }
        }

        // Fall back to owl:versionIRI
        Element versionIRIElement = (Element) doc.getElementsByTagName("owl:versionIRI").item(0);
        if (versionIRIElement != null) {
            String resource = versionIRIElement.getAttribute("rdf:resource");
            Matcher matcher = DATE_PATTERN.matcher(resource);
            if (matcher.find()) {
                return matcher.group(1);
            }
        }

        Events.warn(LOG, "ontology_version_not_found", "file", oboFilePath.toString());
        return null;
    }

    /**
     * Download each specified URL, parse version information from new and current download, and replace current with
     * new if new is newer than current, or if either version cannot be read.
     * <p>
     * Every URL is attempted. A URL that fails (HTTP error, unparseable download) leaves its current file in place
     * and is reported once all URLs have been tried.
     *
     * @param urls        List of URLs to download
     * @param downloadDir Path to directory containing downloaded files
     * @throws IOException          if an I/O error occurs, or if any URL could not be updated
     * @throws InterruptedException if the download is interrupted
     */
    public static void updateDownloads(List<String> urls, Path downloadDir) throws IOException, InterruptedException {
        updateDownloads(urls, downloadDir, HttpClient.newBuilder().followRedirects(Redirect.NORMAL).build());
    }

    /**
     * As {@link #updateDownloads(List, Path)}, using the supplied HTTP client.
     *
     * @param urls        List of URLs to download
     * @param downloadDir Path to directory containing downloaded files
     * @param client      HTTP client used to download
     * @throws IOException          if an I/O error occurs, or if any URL could not be updated
     * @throws InterruptedException if the download is interrupted
     */
    static void updateDownloads(List<String> urls, Path downloadDir, HttpClient client)
            throws IOException, InterruptedException {
        Files.createDirectories(downloadDir);

        long runStarted = System.nanoTime();
        List<String> failures = new ArrayList<>();
        for (String url : urls) {
            URI uri = URI.create(url);
            String path = uri.getPath();
            String fileName = path.substring(path.lastIndexOf('/') + 1);
            String stem = fileName.substring(0, fileName.lastIndexOf('.'));
            String suffix = fileName.substring(fileName.lastIndexOf('.'));
            Path newFile = downloadDir.resolve(stem + "-new" + suffix);

            long started = System.nanoTime();
            try {
                // Download to a temporary file
                HttpRequest request = HttpRequest.newBuilder().uri(uri).build();
                HttpResponse<byte[]> response = client.send(request, HttpResponse.BodyHandlers.ofByteArray());
                if (response.statusCode() != 200) {
                    throw new IOException("HTTP " + response.statusCode() + " for " + url);
                }

                Files.write(newFile, response.body());
                installDownload(newFile, downloadDir, stem, suffix,
                        "url", url, "status_code", response.statusCode(), "bytes", response.body().length,
                        "duration_ms", RunLog.elapsedMs(started));
            } catch (IOException | RuntimeException e) {
                // Leave the current file in place, drop any partial download, and go on to the next URL
                Events.failure(LOG, "ontology_download_failed", e, "ontology", fileName, "url", url,
                        "duration_ms", RunLog.elapsedMs(started));
                Files.deleteIfExists(newFile);
                failures.add(url);
            }
        }

        Events.info(LOG, "ontologies_downloaded", "records_in", urls.size(),
                "records_out", urls.size() - failures.size(), "records_rejected", failures.size(),
                "duration_ms", RunLog.elapsedMs(runStarted));
        if (!failures.isEmpty()) {
            throw new IOException("Could not update " + failures.size() + " of " + urls.size() + " ontologies: "
                    + String.join(", ", failures));
        }
    }

    /**
     * Make a downloaded file current. The new file replaces the current one when its version is newer, or when either
     * version cannot be read, since the download is the latest published and cannot be shown to be stale. The
     * replaced file is archived, never deleted. The new file is removed only when it is provably not newer.
     *
     * @param newFile     The freshly downloaded file
     * @param downloadDir Directory containing the current file
     * @param stem        File name without suffix
     * @param suffix      File name suffix, including the dot
     * @param fields      Alternating key and value arguments describing the download (for example {@code url},
     *                    {@code status_code}, {@code bytes} and {@code duration_ms}), added to the one
     *                    {@code ontology_downloaded}, {@code ontology_updated} or {@code ontology_unchanged} event
     *                    this method logs
     * @throws IOException if a file cannot be moved or deleted
     */
    static void installDownload(Path newFile, Path downloadDir, String stem, String suffix, Object... fields)
            throws IOException {
        String ontology = stem + suffix;
        String versionNew = findOboVersion(newFile);

        Path curFile = downloadDir.resolve(stem + suffix);
        if (!Files.exists(curFile)) {
            Files.move(newFile, curFile);
            Events.info(LOG, "ontology_downloaded", withFields(fields, "ontology", ontology, "version_new", versionNew));
            return;
        }

        String versionCur = findOboVersion(curFile);

        boolean versionsKnown = versionNew != null && versionCur != null;
        if (versionsKnown && versionNew.compareTo(versionCur) <= 0) {
            Files.delete(newFile);
            Events.info(LOG, "ontology_unchanged",
                    withFields(fields, "ontology", ontology, "version_cur", versionCur, "version_new", versionNew));
            return;
        }

        Path archiveDir = downloadDir.resolve(".archive");
        Files.createDirectories(archiveDir);
        String archiveTag = versionCur != null
                ? versionCur
                : "unversioned-" + System.currentTimeMillis() + "-" + UUID.randomUUID();
        Path oldFile = archiveDir.resolve(stem + "-" + archiveTag + suffix);

        Files.move(curFile, oldFile);

        try {
            Files.move(newFile, curFile);
        } catch (IOException e) {
            // The download is intact at newFile; restore the archived file so a
            // failed rename does not also leave curFile missing entirely.
            try {
                Files.move(oldFile, curFile);
            } catch (IOException restoreFailed) {
                e.addSuppressed(restoreFailed);
            }
            throw e;
        }
        Events.info(LOG, "ontology_updated",
                withFields(fields, "ontology", ontology, "version_prev", versionCur, "version_new", versionNew,
                        "archived_to", oldFile.toString()));
    }

    /** The event's own fields followed by the download's. */
    private static Object[] withFields(Object[] download, Object... own) {
        Object[] all = new Object[own.length + download.length];
        System.arraycopy(own, 0, all, 0, own.length);
        System.arraycopy(download, 0, all, own.length, download.length);
        return all;
    }

    /**
     * Download ontology files from the OBO Foundry.
     *
     * @param args (None expected)
     */
    public static void main(String[] args) {
        RunLog.run("OntologyDownloader", () -> updateDownloads(OBO_PURLS, OBO_DIR),
                "obo_dir", OBO_DIR.toString(), "ontologies", OBO_PURLS.size());
    }
}
