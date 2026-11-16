package gov.nih.nlm;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Collects the structured events logged while open, for tests. */
final class LogCapture implements AutoCloseable {

    /** One logged event: its fixed message, its level and its fields. */
    record Event(String message, String level, Map<String, Object> fields) {
        Object get(String field) {
            return fields.get(field);
        }
    }

    private final Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
    private final Level previousLevel = root.getLevel();
    private final ListAppender<ILoggingEvent> appender = new ListAppender<>();

    LogCapture() {
        this(Level.INFO);
    }

    LogCapture(Level level) {
        root.setLevel(level);
        appender.start();
        root.addAppender(appender);
    }

    List<Event> events() {
        return appender.list.stream().map(LogCapture::toEvent).toList();
    }

    /** The events with this message, in order. */
    List<Event> named(String message) {
        return events().stream().filter(e -> e.message().equals(message)).toList();
    }

    /** The one event with this message; fails the test when there is not exactly one. */
    Event one(String message) {
        List<Event> found = named(message);
        if (found.size() != 1) {
            throw new AssertionError("Expected one " + message + " but found " + found.size() + " in " + events());
        }
        return found.get(0);
    }

    private static Event toEvent(ILoggingEvent e) {
        Map<String, Object> fields = new LinkedHashMap<>();
        if (e.getKeyValuePairs() != null) {
            e.getKeyValuePairs().forEach(kv -> fields.put(kv.key, kv.value));
        }
        return new Event(e.getFormattedMessage(), e.getLevel().toString(), fields);
    }

    @Override
    public void close() {
        root.detachAppender(appender);
        root.setLevel(previousLevel);
    }
}
