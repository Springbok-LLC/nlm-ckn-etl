package gov.nih.nlm;

import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

public class InducedSubgraphFinder {

    /**
     * Multi-source BFS up to maxDepth, then extract the induced subgraph.
     */
    public DirectedPseudograph<ArangoVertex, ArangoEdge> find(DirectedPseudograph<ArangoVertex, ArangoEdge> graph,
                                                              String sourceCollection,
                                                              int maxDepth) {

        // Step 1 — Identify source vertices
        Set<ArangoVertex> sources = new HashSet<>();
        for (ArangoVertex v : graph.vertexSet()) {
            if (v.collection().equals(sourceCollection)) {
                sources.add(v);
            }
        }

        System.out.println("Source vertices: " + sources.size());

        // Step 2 — Multi-source BFS with depth limit
        Set<ArangoVertex> reachable = multiBfs(graph, sources, maxDepth);

        System.out.println("Reachable vertices: " + reachable.size());

        // Step 3 — Build induced subgraph
        return inducedSubgraph(graph, reachable);
    }

    private Set<ArangoVertex> multiBfs(DirectedPseudograph<ArangoVertex, ArangoEdge> graph,
                                       Set<ArangoVertex> sources,
                                       int maxDepth) {

        Set<ArangoVertex> visited = new HashSet<>(sources);
        Deque<Map.Entry<ArangoVertex, Integer>> queue = new ArrayDeque<>();

        sources.forEach(s -> queue.add(Map.entry(s, 0)));

        while (!queue.isEmpty()) {
            var entry = queue.poll();
            var node = entry.getKey();
            var depth = entry.getValue();

            if (depth < maxDepth) {
                for (ArangoEdge edge : graph.outgoingEdgesOf(node)) {
                    ArangoVertex neighbor = graph.getEdgeTarget(edge);
                    if (visited.add(neighbor)) {
                        queue.add(Map.entry(neighbor, depth + 1));
                    }
                }
            }
        }

        return visited;
    }

    private DirectedPseudograph<ArangoVertex, ArangoEdge> inducedSubgraph(DirectedPseudograph<ArangoVertex, ArangoEdge> graph,
                                                                          Set<ArangoVertex> vertices) {

        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = new DirectedPseudograph<>(ArangoEdge.class);

        // Add all reachable vertices
        vertices.forEach(induced::addVertex);

        // Add all edges where both endpoints are in the vertex set
        for (ArangoVertex v : vertices) {
            for (ArangoEdge edge : graph.outgoingEdgesOf(v)) {
                ArangoVertex target = graph.getEdgeTarget(edge);
                if (vertices.contains(target)) {
                    induced.addEdge(v, target, edge);
                }
            }
        }

        System.out.println("Induced edges: " + induced.edgeSet().size());

        return induced;
    }
}
