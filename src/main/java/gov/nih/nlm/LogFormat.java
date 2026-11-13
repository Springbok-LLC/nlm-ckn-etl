package gov.nih.nlm;

import ch.qos.logback.core.PropertyDefinerBase;

/**
 * Chooses the appender {@code logback.xml} attaches to the root logger.
 *
 * <p>Only {@code LOG_FORMAT=console} selects readable lines. Any other value, an empty
 * one, or none selects JSON, as docs/logging-conventions.md says. Logback's own
 * {@code ${LOG_FORMAT:-json}} cannot do this: it falls back only when the variable is
 * unset, so {@code LOG_FORMAT=} or {@code LOG_FORMAT=text} would name no appender and
 * the run would log nothing.
 */
public class LogFormat extends PropertyDefinerBase {

    @Override
    public String getPropertyValue() {
        String format = System.getProperty("LOG_FORMAT");
        if (format == null) {
            format = System.getenv("LOG_FORMAT");
        }
        return "console".equals(format) ? "console" : "json";
    }
}
