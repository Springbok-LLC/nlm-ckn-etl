package gov.nih.nlm.model;

import java.util.Map;

/**
 * Represents an ArangoDB edge document.
 *
 * @param key        the {@code _key} field
 * @param from       the original {@code _from} reference (e.g., "CL/0000540")
 * @param to         the original {@code _to} reference (e.g., "GO/0008150")
 * @param collection the edge collection name (e.g., "CL-GO")
 * @param properties all other document fields
 */
public record ArangoEdge(String key,
                         String from,
                         String to,
                         String collection,
                         Map<String, Object> properties) {
}
