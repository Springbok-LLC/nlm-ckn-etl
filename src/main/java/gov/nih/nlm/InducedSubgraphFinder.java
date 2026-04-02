package gov.nih.nlm;

import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.stream.Collectors;

public class InducedSubgraphFinder {

    // Ontology hierarchy traversal configuration
    // "all": include entire vertex and edge collection (CL special case)
    // "walk": BFS walk following edges with the given Label to root
    private record HierarchyConfig(String strategy, String label) {}

    // Collections to skip during BFS traversal
    private static final Set<String> IGNORED_COLLECTIONS = Set.of("CHEBI", "NCT");

    private static final Map<String, HierarchyConfig> HIERARCHY_CONFIG = Map.ofEntries(
            Map.entry("CL", new HierarchyConfig("all", null)),
            Map.entry("GO", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("MONDO", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("HP", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("PATO", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("HsapDv", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("NCBITaxon", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("Orphanet", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("PR", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("CHEBI", new HierarchyConfig("walk", "SUB_CLASS_OF")),
            Map.entry("UBERON", new HierarchyConfig("walk", "PART_OF"))
    );

    private record HierarchyResult(Set<ArangoVertex> ancestors, Set<ArangoEdge> edges) {}

    /**
     * Multi-source BFS up to maxDepth, then extract the induced subgraph.
     *
     * @param graph            the full source graph
     * @param sourceCollection the vertex collection to start BFS from (e.g., "CS")
     * @param maxDepth         maximum BFS traversal depth
     * @return the induced subgraph containing all reachable vertices and their connecting edges
     */
    public DirectedPseudograph<ArangoVertex, ArangoEdge> find(DirectedPseudograph<ArangoVertex, ArangoEdge> graph,
                                                              String sourceCollection,
                                                              int maxDepth) {

        // Identify source vertices
        Set<ArangoVertex> sources = new HashSet<>();
        for (ArangoVertex v : graph.vertexSet()) {
            if (v.collection().equals(sourceCollection)) {
                sources.add(v);
            }
        }

        System.out.println("Source vertices: " + sources.size());

        // Multi-source BFS with depth limit
        Set<ArangoVertex> reachable = multiBfs(graph, sources, maxDepth);

        System.out.println("Reachable vertices: " + reachable.size());

        // Build induced subgraph
        return inducedSubgraph(graph, reachable);
    }

    /**
     * For every ontology vertex in the induced subgraph, add hierarchy
     * paths (child->parent to root) based on HIERARCHY_CONFIG rules.
     *
     * @param fullGraph the full source graph
     * @param induced   the induced subgraph to enrich with hierarchy paths
     */
    public void addOntologyHierarchyPaths(DirectedPseudograph<ArangoVertex, ArangoEdge> fullGraph,
                                          DirectedPseudograph<ArangoVertex, ArangoEdge> induced) {

        // Identify which ontology prefixes are present in the induced subgraph
        Set<String> prefixesPresent = induced.vertexSet().stream()
                .map(ArangoVertex::collection)
                .filter(HIERARCHY_CONFIG::containsKey)
                .collect(Collectors.toCollection(TreeSet::new));

        Set<ArangoVertex> verticesToAdd = new HashSet<>();
        Set<ArangoEdge> edgesToAdd = new HashSet<>();

        for (String prefix : prefixesPresent) {
            HierarchyConfig config = HIERARCHY_CONFIG.get(prefix);
            System.out.println("Adding hierarchy paths for " + prefix + " (strategy: " + config.strategy() + ")");

            if ("all".equals(config.strategy())) {
                HierarchyResult result = collectAllOntologyNodesAndEdges(fullGraph, prefix);
                verticesToAdd.addAll(result.ancestors());
                edgesToAdd.addAll(result.edges());

            } else if ("walk".equals(config.strategy())) {
                Set<ArangoVertex> ontologyVertices = induced.vertexSet().stream()
                        .filter(v -> v.collection().equals(prefix))
                        .collect(Collectors.toSet());

                for (ArangoVertex vertex : ontologyVertices) {
                    HierarchyResult result = walkHierarchyToRoot(fullGraph, vertex, prefix, config.label());
                    verticesToAdd.addAll(result.ancestors());
                    edgesToAdd.addAll(result.edges());
                }
            }
        }

        // Add ancestor vertices
        for (ArangoVertex v : verticesToAdd) {
            if (!induced.containsVertex(v)) {
                induced.addVertex(v);
            }
        }

        // Add hierarchy edges
        for (ArangoEdge edge : edgesToAdd) {
            if (!induced.containsEdge(edge)) {
                ArangoVertex source = findVertex(fullGraph, edge.from());
                ArangoVertex target = findVertex(fullGraph, edge.to());
                if (source != null && target != null) {
                    induced.addEdge(source, target, edge);
                }
            }
        }

        System.out.println("After hierarchy enrichment:");
        System.out.println("  Vertices: " + induced.vertexSet().size());
        System.out.println("  Edges:    " + induced.edgeSet().size());
    }

    /**
     * BFS walk from startNode to root, following only self-referential
     * edges (e.g., GO-GO) with the specified Label.
     */
    private HierarchyResult walkHierarchyToRoot(DirectedPseudograph<ArangoVertex, ArangoEdge> fullGraph,
                                                ArangoVertex startNode,
                                                String ontologyPrefix,
                                                String labelFilter) {

        String edgeCollection = ontologyPrefix + "-" + ontologyPrefix;
        Set<ArangoVertex> ancestors = new HashSet<>();
        Set<ArangoEdge> hierarchyEdges = new HashSet<>();
        Set<ArangoVertex> visited = new HashSet<>();
        visited.add(startNode);
        Deque<ArangoVertex> queue = new ArrayDeque<>();
        queue.add(startNode);

        while (!queue.isEmpty()) {
            ArangoVertex node = queue.poll();
            for (ArangoEdge edge : fullGraph.outgoingEdgesOf(node)) {
                if (!edge.collection().equals(edgeCollection)) continue;
                if (!labelFilter.equals(edge.properties().get("Label"))) continue;

                ArangoVertex target = fullGraph.getEdgeTarget(edge);
                if (!target.collection().equals(ontologyPrefix)) continue;

                hierarchyEdges.add(edge);
                if (visited.add(target)) {
                    ancestors.add(target);
                    queue.add(target);
                }
            }
        }

        return new HierarchyResult(ancestors, hierarchyEdges);
    }

    /**
     * Collect ALL vertices with the given prefix and ALL edges in the
     * self-referential edge collection (e.g., CL-CL).
     */
    private HierarchyResult collectAllOntologyNodesAndEdges(DirectedPseudograph<ArangoVertex, ArangoEdge> fullGraph,
                                                            String ontologyPrefix) {

        String edgeCollection = ontologyPrefix + "-" + ontologyPrefix;

        Set<ArangoVertex> allNodes = fullGraph.vertexSet().stream()
                .filter(v -> v.collection().equals(ontologyPrefix))
                .collect(Collectors.toSet());

        Set<ArangoEdge> allEdges = fullGraph.edgeSet().stream()
                .filter(e -> e.collection().equals(edgeCollection))
                .collect(Collectors.toSet());

        return new HierarchyResult(allNodes, allEdges);
    }

    /**
     * Returns true if the edge connects two vertices in the same ontology collection
     * (e.g., CL-CL, GO-GO). These are skipped during BFS since hierarchy enrichment
     * handles them separately.
     */
    private boolean isSelfReferentialEdge(ArangoEdge edge) {
        String col = edge.collection();
        int dash = col.indexOf('-');
        return dash > 0 && col.substring(0, dash).equals(col.substring(dash + 1));
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
                    if (isSelfReferentialEdge(edge)) continue;
                    ArangoVertex neighbor = graph.getEdgeTarget(edge);
                    if (!IGNORED_COLLECTIONS.contains(neighbor.collection()) && visited.add(neighbor)) {
                        queue.add(Map.entry(neighbor, depth + 1));
                    }
                }
                for (ArangoEdge edge : graph.incomingEdgesOf(node)) {
                    if (isSelfReferentialEdge(edge)) continue;
                    ArangoVertex neighbor = graph.getEdgeSource(edge);
                    if (!IGNORED_COLLECTIONS.contains(neighbor.collection()) && visited.add(neighbor)) {
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

    /**
     * Find a vertex in the graph by its full ArangoDB ID (e.g., "GO/0008150").
     */
    private ArangoVertex findVertex(DirectedPseudograph<ArangoVertex, ArangoEdge> graph, String id) {
        for (ArangoVertex v : graph.vertexSet()) {
            if (v.id().equals(id)) return v;
        }
        return null;
    }
}
