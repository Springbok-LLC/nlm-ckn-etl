package gov.nih.nlm;

import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.joran.JoranConfigurator;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.net.URL;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The shape of the JSON lines, and the run lifecycle, against the real logback.xml. */
class RunLogTest {

    private static final Logger LOG = LoggerFactory.getLogger(RunLogTest.class);
    private static final ObjectMapper JSON = new ObjectMapper();

    private final PrintStream originalOut = System.out;
    private ByteArrayOutputStream captured;

    @BeforeEach
    void captureStdout() throws Exception {
        captured = new ByteArrayOutputStream();
        System.setOut(new PrintStream(captured, true));
        configure(null);
    }

    @AfterEach
    void restore() throws Exception {
        System.setOut(originalOut);
        System.clearProperty("LOG_FORMAT");
        MDC.clear();
        configure(null);
    }

    private static void configure(String logFormat) throws Exception {
        if (logFormat == null) {
            System.clearProperty("LOG_FORMAT");
        } else {
            System.setProperty("LOG_FORMAT", logFormat);
        }
        LoggerContext context = (LoggerContext) LoggerFactory.getILoggerFactory();
        context.reset();
        URL config = RunLogTest.class.getResource("/logback.xml");
        assertNotNull(config);
        JoranConfigurator configurator = new JoranConfigurator();
        configurator.setContext(context);
        configurator.doConfigure(config);
    }

    private List<JsonNode> lines() throws Exception {
        List<JsonNode> lines = new java.util.ArrayList<>();
        for (String line : captured.toString().split("\n")) {
            if (!line.isBlank()) {
                lines.add(JSON.readTree(line));
            }
        }
        return lines;
    }

    @Test
    void eventCarriesCoreFieldsAndTypedPayload() throws Exception {
        RunLog.init(Map.of("CORRELATION_ID", "abc", "GIT_SHA", "deadbee", "PHASE", "ontology"));
        Events.info(LOG, "vertices_inserted", "collection", "CL", "records_out", 12, "duration_ms", 34L);

        JsonNode line = lines().get(0);
        assertEquals("vertices_inserted", line.get("message").asText());
        assertEquals("INFO", line.get("level").asText());
        assertEquals("etl-pipeline", line.get("service").asText());
        assertEquals("abc", line.get("correlation_id").asText());
        assertEquals("deadbee", line.get("release").asText());
        assertEquals("ontology", line.get("phase").asText());
        assertEquals("gov.nih.nlm.RunLogTest", line.get("logger").asText());
        assertEquals("CL", line.get("collection").asText());
        assertTrue(line.get("records_out").isNumber());
        assertEquals(12, line.get("records_out").asInt());
        assertTrue(line.get("timestamp").asText().matches("\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z"));
        assertFalse(line.has("thread_name"));
        assertFalse(line.has("@version"));
        assertFalse(line.has("level_value"));
    }

    @Test
    void missingEnvironmentGetsAFreshCorrelationIdAndUnknownRelease() throws Exception {
        RunLog.init(Map.of());
        Events.warn(LOG, "something_degraded");

        JsonNode line = lines().get(0);
        assertEquals("WARN", line.get("level").asText());
        assertFalse(line.get("correlation_id").asText().isBlank());
        assertEquals("unknown", line.get("release").asText());
        assertFalse(line.has("phase"));
    }

    @Test
    void failureCarriesErrorAndStackTrace() throws Exception {
        RunLog.init(Map.of());
        Events.failure(LOG, "insert_failed", new IllegalStateException("boom"), "collection", "CL");

        JsonNode line = lines().get(0);
        assertEquals("ERROR", line.get("level").asText());
        assertEquals("IllegalStateException", line.get("error_type").asText());
        assertEquals("boom", line.get("error").asText());
        assertTrue(line.get("exception").asText().contains("IllegalStateException: boom"));
    }

    @Test
    void nullFieldsAreLeftOut() throws Exception {
        Events.info(LOG, "x", "present", 1, "absent", null);
        JsonNode line = lines().get(0);
        assertEquals(1, line.get("present").asInt());
        assertFalse(line.has("absent"));
    }

    @Test
    void debugIsOffByDefault() throws Exception {
        Events.debug(LOG, "per_item_detail", "id", "x");
        assertTrue(lines().isEmpty());
    }

    @Test
    void oddFieldArgumentsAreRejected() {
        assertThrows(IllegalArgumentException.class, () -> Events.info(LOG, "x", "key_without_value"));
    }

    @Test
    void successfulRunIsStartedThenFinished() throws Exception {
        RunLog.run("Demo", () -> Events.info(LOG, "work_done", "records_out", 3), "args", "a b");

        List<JsonNode> lines = lines();
        assertEquals(List.of("run_started", "work_done", "run_finished"),
                lines.stream().map(l -> l.get("message").asText()).toList());
        assertEquals("Demo", lines.get(0).get("script").asText());
        assertEquals("a b", lines.get(0).get("args").asText());
        JsonNode finished = lines.get(2);
        assertEquals("ok", finished.get("outcome").asText());
        assertTrue(finished.get("duration_ms").isNumber());
    }

    @Test
    void failedRunLogsTheErrorAndRethrowsIt() throws Exception {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> RunLog.run("Demo", () -> {
                    throw new IllegalStateException("no input");
                }));
        assertEquals("no input", thrown.getMessage());

        List<JsonNode> lines = lines();
        assertEquals(List.of("run_started", "run_failed"),
                lines.stream().map(l -> l.get("message").asText()).toList());
        JsonNode failed = lines.get(1);
        assertEquals("ERROR", failed.get("level").asText());
        assertEquals("failed", failed.get("outcome").asText());
        assertEquals("IllegalStateException", failed.get("error_type").asText());
    }

    @Test
    void checkedExceptionIsWrappedAfterBeingLogged() throws Exception {
        RuntimeException thrown = assertThrows(RuntimeException.class,
                () -> RunLog.run("Demo", () -> {
                    throw new java.io.IOException("disk");
                }));
        assertEquals("disk", thrown.getCause().getMessage());
        assertEquals("run_failed", lines().get(1).get("message").asText());
    }

    @Test
    void consoleFormatIsReadable() throws Exception {
        configure("console");
        Events.info(LOG, "vertices_inserted", "records_out", 12);

        String out = captured.toString();
        assertTrue(out.contains("INFO"), out);
        assertTrue(out.contains("[RunLogTest] vertices_inserted"), out);
        assertTrue(out.contains("records_out"), out);
        assertFalse(out.trim().startsWith("{"), out);
    }
}
