package gov.nih.nlm;

import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * @deprecated Tests for deprecated {@link PhenotypeGraphBuilder}.
 */
@Deprecated
@SuppressWarnings("deprecation")
class PhenotypeGraphBuilderTest {

    private static Map<String, Object> makeVertexMap(String id, String key) {
        LinkedHashMap<String, Object> map = new LinkedHashMap<>();
        map.put("_id", id + "/" + key);
        map.put("_key", key);
        return map;
    }

    private static Map<String, Object> makeEdgeMap(String id, String key, String from, String to) {
        LinkedHashMap<String, Object> map = new LinkedHashMap<>();
        map.put("_id", id + "/" + key);
        map.put("_key", key);
        map.put("_from", from);
        map.put("_to", to);
        return map;
    }

    @Test
    void getVertexDocuments_collectsUniqueVertices() {
        // Build two paths that share a vertex (CL/0000235)
        List<Map<String, Object>> paths = new ArrayList<>();

        Map<String, Object> path1 = new LinkedHashMap<>();
        ArrayList<LinkedHashMap> vertices1 = new ArrayList<>();
        vertices1.add(new LinkedHashMap<>(makeVertexMap("CL", "0000235")));
        vertices1.add(new LinkedHashMap<>(makeVertexMap("GO", "0031268")));
        path1.put("vertices", vertices1);
        path1.put("edges", new ArrayList<>());
        paths.add(path1);

        Map<String, Object> path2 = new LinkedHashMap<>();
        ArrayList<LinkedHashMap> vertices2 = new ArrayList<>();
        vertices2.add(new LinkedHashMap<>(makeVertexMap("CL", "0000235"))); // duplicate
        vertices2.add(new LinkedHashMap<>(makeVertexMap("UBERON", "0000061")));
        path2.put("vertices", vertices2);
        path2.put("edges", new ArrayList<>());
        paths.add(path2);

        List<BaseDocument> result = PhenotypeGraphBuilder.getVertexDocuments(paths);

        // Should deduplicate CL/0000235
        assertEquals(3, result.size());
    }

    @Test
    void getVertexDocuments_emptyPaths() {
        List<BaseDocument> result = PhenotypeGraphBuilder.getVertexDocuments(List.of());
        assertTrue(result.isEmpty());
    }

    @Test
    void getEdgeDocuments_collectsUniqueEdges() {
        // Build two paths that share an edge (CL-GO/0000235-0031268)
        List<Map<String, Object>> paths = new ArrayList<>();

        Map<String, Object> path1 = new LinkedHashMap<>();
        path1.put("vertices", new ArrayList<>());
        ArrayList<LinkedHashMap> edges1 = new ArrayList<>();
        edges1.add(new LinkedHashMap<>(makeEdgeMap("CL-GO", "0000235-0031268", "CL/0000235", "GO/0031268")));
        path1.put("edges", edges1);
        paths.add(path1);

        Map<String, Object> path2 = new LinkedHashMap<>();
        path2.put("vertices", new ArrayList<>());
        ArrayList<LinkedHashMap> edges2 = new ArrayList<>();
        edges2.add(new LinkedHashMap<>(makeEdgeMap("CL-GO", "0000235-0031268", "CL/0000235", "GO/0031268"))); // duplicate
        edges2.add(new LinkedHashMap<>(makeEdgeMap("CL-UBERON", "0000235-0000061", "CL/0000235", "UBERON/0000061")));
        path2.put("edges", edges2);
        paths.add(path2);

        List<BaseEdgeDocument> result = PhenotypeGraphBuilder.getEdgeDocuments(paths);

        // Should deduplicate CL-GO/0000235-0031268
        assertEquals(2, result.size());
    }

    @Test
    void getEdgeDocuments_emptyPaths() {
        List<BaseEdgeDocument> result = PhenotypeGraphBuilder.getEdgeDocuments(List.of());
        assertTrue(result.isEmpty());
    }
}
