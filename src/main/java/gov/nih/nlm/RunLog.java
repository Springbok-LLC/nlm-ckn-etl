package gov.nih.nlm;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

import java.util.Map;
import java.util.UUID;

/**
 * Run-level logging for a {@code main()}: binds the run context and logs the
 * run as {@code run_started}, then {@code run_finished} or {@code run_failed}.
 *
 * <p>The Prefect flow that launches a class passes {@code CORRELATION_ID},
 * {@code GIT_SHA} and {@code PHASE} in the environment, so the Java lines join
 * the rest of the run. A fresh UUID stands in for a missing correlation id, so
 * a line run by hand still groups with the others from that process.
 */
public final class RunLog {

    private static final Logger LOG = LoggerFactory.getLogger(RunLog.class);

    private RunLog() {}

    /** A {@code main()} body. */
    @FunctionalInterface
    public interface Body {
        void run() throws Exception;
    }

    /** Binds {@code correlation_id}, {@code release} and {@code phase} from the environment. */
    public static void init() {
        init(System.getenv());
    }

    /**
     * Binds {@code correlation_id}, {@code release} and {@code phase} from
     * {@code env}. {@code release} is {@code unknown} when {@code GIT_SHA} is
     * unset, and {@code phase} is left out when {@code PHASE} is unset.
     */
    static void init(Map<String, String> env) {
        MDC.put("correlation_id", valueOrDefault(env.get("CORRELATION_ID"), UUID.randomUUID().toString()));
        MDC.put("release", valueOrDefault(env.get("GIT_SHA"), "unknown"));
        String phase = env.get("PHASE");
        if (phase != null && !phase.isBlank()) {
            MDC.put("phase", phase);
        } else {
            MDC.remove("phase");
        }
    }

    /**
     * Runs {@code body} as the run of {@code script}.
     *
     * <p>Logs {@code run_started} with {@code fields} (the effective
     * configuration and arguments), then {@code run_finished} with
     * {@code outcome=ok} and {@code duration_ms}. If the body throws, logs
     * {@code run_failed} with the exception and rethrows it, so the exit status
     * is unchanged.
     *
     * @param script the main class's simple name
     * @param fields alternating key and value arguments for {@code run_started}
     * @param body the work
     */
    public static void run(String script, Body body, Object... fields) {
        init();
        long started = System.nanoTime();
        Object[] startedFields = new Object[fields.length + 2];
        startedFields[0] = "script";
        startedFields[1] = script;
        System.arraycopy(fields, 0, startedFields, 2, fields.length);
        Events.info(LOG, "run_started", startedFields);
        try {
            body.run();
        } catch (Throwable t) {
            Events.failure(LOG, "run_failed", t, "script", script, "outcome", "failed",
                    "duration_ms", elapsedMs(started));
            if (t instanceof RuntimeException e) {
                throw e;
            }
            if (t instanceof Error e) {
                throw e;
            }
            throw new RuntimeException(t);
        }
        Events.info(LOG, "run_finished", "script", script, "outcome", "ok", "duration_ms", elapsedMs(started));
    }

    /** Milliseconds since {@code startedNanos}, from {@link System#nanoTime()}. */
    public static long elapsedMs(long startedNanos) {
        return (System.nanoTime() - startedNanos) / 1_000_000;
    }

    private static String valueOrDefault(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }
}
