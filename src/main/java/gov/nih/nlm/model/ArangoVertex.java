package gov.nih.nlm.model;

import java.util.Map;

/**
 * Represents an ArangoDB vertex document.
 *
 * @param id         the full ArangoDB {@code _id} (e.g., "CL/0000540")
 * @param collection the vertex collection name (e.g., "CL")
 * @param key        the {@code _key} field
 * @param properties all other document fields
 */
public record ArangoVertex(String id,
                           String collection,
                           String key,
                           Map<String, Object> properties) {
}
