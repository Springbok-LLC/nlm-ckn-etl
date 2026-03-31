package gov.nih.nlm.model;

import java.util.Map;

public record ArangoVertex(String id,          // full ArangoDB _id e.g. "myCollection/myKey"
                           String collection,  // collection name
                           String key,         // _key
                           Map<String, Object> properties  // all other document fields
) {
}
