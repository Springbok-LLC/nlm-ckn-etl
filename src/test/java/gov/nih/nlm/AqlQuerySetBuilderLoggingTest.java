package gov.nih.nlm;

import ch.qos.logback.classic.Level;
import com.arangodb.ArangoCursor;
import com.arangodb.ArangoDatabase;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** The events the AqlQuerySetBuilder dev harness logs, over a database that returns canned paths. */
class AqlQuerySetBuilderLoggingTest {

    private static ArangoDatabase database(Map<String, List<Map<String, Object>>> pathsByQuery) {
        return (ArangoDatabase) Proxy.newProxyInstance(AqlQuerySetBuilderLoggingTest.class.getClassLoader(),
                new Class<?>[]{ArangoDatabase.class}, (proxy, method, args) -> {
                    if (!"query".equals(method.getName())) {
                        return null;
                    }
                    List<Map<String, Object>> paths = pathsByQuery.get((String) args[0]);
                    return Proxy.newProxyInstance(AqlQuerySetBuilderLoggingTest.class.getClassLoader(),
                            new Class<?>[]{ArangoCursor.class},
                            (p, m, a) -> "asListRemaining".equals(m.getName()) ? paths : null);
                });
    }

    @Test
    void eachQueryIsOneEventAndAnEmptyOneWarns() {
        Map<String, AqlQuerySetBuilder.AqlQuerySet> sets = new LinkedHashMap<>();
        sets.put("CS-BGS", new AqlQuerySetBuilder.AqlQuerySet(Map.of(), "FOR v IN\nBGS RETURN v"));
        sets.put("CS-CL-GO", new AqlQuerySetBuilder.AqlQuerySet(Map.of(), "FOR v IN CL RETURN v"));
        ArangoDatabase db = database(Map.of(
                "FOR v IN\nBGS RETURN v", List.of(Map.of(), Map.of()),
                "FOR v IN CL RETURN v", List.of()));
        try (LogCapture logs = new LogCapture()) {
            AqlQuerySetBuilder.runQueries(db, sets);

            List<LogCapture.Event> collected = logs.named("aql_paths_collected");
            assertEquals(2, collected.size());
            assertEquals("CS-BGS", collected.get(0).get("query_id"));
            assertEquals(2, collected.get(0).get("records_out"));
            assertEquals(0, collected.get(1).get("records_out"));
            assertTrue(collected.get(0).get("duration_ms") instanceof Long);
            LogCapture.Event empty = logs.one("aql_paths_empty");
            assertEquals("WARN", empty.level());
            assertEquals("CS-CL-GO", empty.get("query_id"));
            assertTrue(logs.named("aql_query").isEmpty());
        }
    }

    @Test
    void queryTextIsOnlyDebugWithItsWhitespaceCollapsed() {
        Map<String, AqlQuerySetBuilder.AqlQuerySet> sets = Map.of("CS-BGS",
                new AqlQuerySetBuilder.AqlQuerySet(Map.of(), "FOR v IN\nBGS RETURN v"));
        ArangoDatabase db = database(Map.of("FOR v IN\nBGS RETURN v", List.of(Map.of())));
        try (LogCapture logs = new LogCapture(Level.DEBUG)) {
            AqlQuerySetBuilder.runQueries(db, sets);

            LogCapture.Event e = logs.one("aql_query");
            assertEquals("DEBUG", e.level());
            assertEquals("FOR v IN BGS RETURN v", e.get("query"));
            assertFalse(logs.named("aql_paths_collected").isEmpty());
        }
    }
}
