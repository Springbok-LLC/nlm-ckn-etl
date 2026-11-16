package gov.nih.nlm;

import org.slf4j.Logger;
import org.slf4j.event.Level;

/**
 * Structured log events: a fixed {@code snake_case} message plus fields.
 *
 * <p>The message names the kind of event and never carries a count, path or id;
 * those go in fields, given as alternating key and value arguments:
 *
 * <pre>{@code
 * Events.info(LOG, "vertices_inserted", "collection", name, "records_out", n);
 * }</pre>
 *
 * <p>A field is a top-level JSON member, and a number stays a number. A field whose value is null is left out,
 * so no line carries a {@code null}. The
 * vocabulary is in {@code docs/logging-conventions.md}.
 */
public final class Events {

    private Events() {}

    /** Logs an event at DEBUG. */
    public static void debug(Logger log, String message, Object... fields) {
        emit(log, Level.DEBUG, message, null, fields);
    }

    /** Logs an event at INFO. */
    public static void info(Logger log, String message, Object... fields) {
        emit(log, Level.INFO, message, null, fields);
    }

    /** Logs an event at WARN. */
    public static void warn(Logger log, String message, Object... fields) {
        emit(log, Level.WARN, message, null, fields);
    }

    /** Logs an event at ERROR. */
    public static void error(Logger log, String message, Object... fields) {
        emit(log, Level.ERROR, message, null, fields);
    }

    /**
     * Logs a failure at ERROR with {@code error_type} and {@code error} fields
     * and the stack trace in {@code exception}.
     */
    public static void failure(Logger log, String message, Throwable cause, Object... fields) {
        emit(log, Level.ERROR, message, cause, fields);
    }

    private static void emit(Logger log, Level level, String message, Throwable cause, Object[] fields) {
        if (fields.length % 2 != 0) {
            throw new IllegalArgumentException("Fields for " + message + " must be key and value pairs");
        }
        if (!log.isEnabledForLevel(level)) {
            return;
        }
        var builder = log.atLevel(level).setMessage(message);
        for (int i = 0; i < fields.length; i += 2) {
            if (fields[i + 1] != null) {
                builder = builder.addKeyValue(String.valueOf(fields[i]), fields[i + 1]);
            }
        }
        if (cause != null) {
            builder = builder
                    .addKeyValue("error_type", cause.getClass().getSimpleName())
                    .addKeyValue("error", String.valueOf(cause.getMessage()))
                    .setCause(cause);
        }
        builder.log();
    }
}
