package gov.nih.nlm.model;

import java.util.Map;

public record ArangoEdge(String key,         // _key
                         String from,        // original _from
                         String to,          // original _to
                         Map<String, Object> properties  // all other document fields
) {
}
