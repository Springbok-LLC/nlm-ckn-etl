package gov.nih.nlm;

import gov.nih.nlm.model.ArangoEdge;
import gov.nih.nlm.model.ArangoVertex;
import org.jgrapht.graph.DirectedPseudograph;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.stream.Collectors;

public class InducedSubgraphFinder {

    private static final Logger LOG = LoggerFactory.getLogger(InducedSubgraphFinder.class);

    // Vertex collections to skip during BFS traversal. GO, HsapDv and PATO are excluded because
    // their terms and associations, as carried into the induced phenotype graph, do not provide
    // meaningful biological information (GO: Springbok-LLC/nlm-ckn-etl#119; HsapDv and PATO:
    // Springbok-LLC/nlm-ckn-etl#155).
    private static final Set<String> IGNORED_VERTEX_COLLECTIONS =
            Set.of("CHEBI", "GO", "HsapDv", "NCT", "PATO");

    // Edge collections to skip entirely during BFS traversal. Use this to cut specific
    // high-fanout connections (e.g., PR-NCBITaxon pulls in most of UniProt's PR vertices
    // because every protein is annotated with its source organism).
    private static final Set<String> IGNORED_EDGE_COLLECTIONS = Set.of("PR-NCBITaxon");
    // No entry for GO, HsapDv or PATO: they are excluded above, so they never reach the induced
    // graph for this config to apply to.
    private static final Map<String, HierarchyConfig> HIERARCHY_CONFIG = Map.ofEntries(Map.entry("CL",
                    new HierarchyConfig("all", Set.of())),
            Map.entry("MONDO", new HierarchyConfig("walk", Set.of("SUB_CLASS_OF"))),
            Map.entry("HP", new HierarchyConfig("walk", Set.of("SUB_CLASS_OF"))),
            Map.entry("NCBITaxon", new HierarchyConfig("walk", Set.of("SUB_CLASS_OF"))),
            Map.entry("Orphanet", new HierarchyConfig("walk", Set.of("SUB_CLASS_OF"))),
            Map.entry("PR", new HierarchyConfig("none", Set.of())),
            Map.entry("CHEBI", new HierarchyConfig("walk", Set.of("SUB_CLASS_OF"))),
            Map.entry("UBERON",
                    new HierarchyConfig("walk", Set.of("PART_OF", "CONTRIBUTES_TO_MORPHOLOGY_OF"))));

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

        long started = System.nanoTime();

        // Identify source vertices
        Set<ArangoVertex> sources = new HashSet<>();
        for (ArangoVertex v : graph.vertexSet()) {
            if (v.collection().equals(sourceCollection)) {
                sources.add(v);
            }
        }

        if (sources.isEmpty()) {
            Events.warn(LOG, "induced_subgraph_empty", "reason", "no_source_vertices", "source_collection",
                    sourceCollection);
        }

        // Multi-source BFS with depth limit
        Set<ArangoVertex> reachable = multiBfs(graph, sources, maxDepth);

        // Build induced subgraph
        DirectedPseudograph<ArangoVertex, ArangoEdge> induced = inducedSubgraph(graph, reachable);
        Events.info(LOG, "induced_subgraph_extracted", "records_in", graph.vertexSet().size(),
                "source_collection", sourceCollection, "source_vertices", sources.size(),
                "reachable_vertices", reachable.size(), "records_out", induced.edgeSet().size(),
                "max_depth", maxDepth, "duration_ms", RunLog.elapsedMs(started));
        return induced;
    }

    /**
     * For every ontology vertex in the induced subgraph, add hierarchy paths (child->parent to root) based on
     * HIERARCHY_CONFIG rules.
     *
     * @param fullGraph the full source graph
     * @param induced   the induced subgraph to enrich with hierarchy paths
     */
    public void addOntologyHierarchyPaths(DirectedPseudograph<ArangoVertex, ArangoEdge> fullGraph,
                                          DirectedPseudograph<ArangoVertex, ArangoEdge> induced) {

        // Identify which ontology prefixes are present in the induced subgraph
        Set<String> prefixesPresent = induced.vertexSet().stream().map(ArangoVertex::collection).filter(HIERARCHY_CONFIG::containsKey).collect(
                Collectors.toCollection(TreeSet::new));

        Set<ArangoVertex> verticesToAdd = new HashSet<>();
        Set<ArangoEdge> edgesToAdd = new HashSet<>();

        for (String prefix : prefixesPresent) {
            HierarchyConfig config = HIERARCHY_CONFIG.get(prefix);
            if ("none".equals(config.strategy)) {
                Events.debug(LOG, "hierarchy_skipped", "prefix", prefix, "strategy", config.strategy());
                continue;
            }
            long started = System.nanoTime();
            int verticesBefore = verticesToAdd.size();
            int edgesBefore = edgesToAdd.size();

            if ("all".equals(config.strategy())) {
                HierarchyResult result = collectAllOntologyNodesAndEdges(fullGraph, prefix);
                verticesToAdd.addAll(result.ancestors());
                edgesToAdd.addAll(result.edges());

            } else if ("walk".equals(config.strategy())) {
                Set<ArangoVertex> ontologyVertices = induced.vertexSet().stream().filter(v -> v.collection().equals(
                        prefix)).collect(Collectors.toSet());

                for (ArangoVertex vertex : ontologyVertices) {
                    HierarchyResult result = walkHierarchyToRoot(fullGraph, vertex, prefix, config.labels());
                    verticesToAdd.addAll(result.ancestors());
                    edgesToAdd.addAll(result.edges());
                }
            }
            Events.info(LOG, "hierarchy_enriched", "prefix", prefix, "strategy", config.strategy(),
                    "labels", config.labels().isEmpty() ? null : new TreeSet<>(config.labels()),
                    "vertices_added", verticesToAdd.size() - verticesBefore,
                    "edges_added", edgesToAdd.size() - edgesBefore, "duration_ms", RunLog.elapsedMs(started));
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

    }

    /**
     * BFS walk from startNode to root, following only self-referential edges (e.g., GO-GO) whose Label is one of the
     * specified labels. The labels are followed together, so a walk can mix them at every step.
     */
    private HierarchyResult walkHierarchyToRoot(DirectedPseudograph<ArangoVertex, ArangoEdge> fullGraph,
                                                ArangoVertex startNode,
                                                String ontologyPrefix,
                                                Set<String> labels) {

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
                // Set.of(...).contains(null) throws, and an edge need not carry a Label
                Object label = edge.properties().get("Label");
                if (label == null || !labels.contains(label)) continue;

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
     * Collect ALL vertices with the given prefix and ALL edges in the self-referential edge collection (e.g., CL-CL).
     */
    private HierarchyResult collectAllOntologyNodesAndEdges(DirectedPseudograph<ArangoVertex, ArangoEdge> fullGraph,
                                                            String ontologyPrefix) {

        String edgeCollection = ontologyPrefix + "-" + ontologyPrefix;

        Set<ArangoVertex> allNodes = fullGraph.vertexSet().stream().filter(v -> v.collection().equals(ontologyPrefix)).collect(
                Collectors.toSet());

        Set<ArangoEdge> allEdges = fullGraph.edgeSet().stream().filter(e -> e.collection().equals(edgeCollection)).collect(
                Collectors.toSet());

        return new HierarchyResult(allNodes, allEdges);
    }

    /**
     * Returns true if the edge connects two vertices in the same ontology collection (e.g., CL-CL, GO-GO). These are
     * skipped during BFS since hierarchy enrichment handles them separately.
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
                    if (IGNORED_EDGE_COLLECTIONS.contains(edge.collection())) continue;
                    ArangoVertex neighbor = graph.getEdgeTarget(edge);
                    if (!IGNORED_VERTEX_COLLECTIONS.contains(neighbor.collection()) && visited.add(neighbor)) {
                        queue.add(Map.entry(neighbor, depth + 1));
                    }
                }
                for (ArangoEdge edge : graph.incomingEdgesOf(node)) {
                    if (isSelfReferentialEdge(edge)) continue;
                    if (IGNORED_EDGE_COLLECTIONS.contains(edge.collection())) continue;
                    ArangoVertex neighbor = graph.getEdgeSource(edge);
                    if (!IGNORED_VERTEX_COLLECTIONS.contains(neighbor.collection()) && visited.add(neighbor)) {
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

    // Ontology hierarchy traversal configuration
    // "all": include entire vertex and edge collection (CL special case)
    // "walk": BFS walk following edges with any of the given labels to root
    // "none": no hierarchy paths are added
    // The labels are used only by "walk"; "all" and "none" take an empty set.
    private record HierarchyConfig(String strategy, Set<String> labels) {
    }

    private record HierarchyResult(Set<ArangoVertex> ancestors, Set<ArangoEdge> edges) {
    }
}
