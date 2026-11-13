package gov.nih.nlm;

import org.slf4j.Logger;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * Decides which edges between cell types (CL) and anatomical structures (UBERON) to keep, and counts those dropped.
 * <p>
 * Only the CL-UBERON and UBERON-CL edge collections are filtered, and only by an explicit keep-list of normalized edge
 * labels, reviewed for each direction. A label that is not on the list is dropped, including one introduced by a
 * later ontology release, so that unreviewed relations do not enter the graph; the drops are counted so they are
 * visible. Edges between all other pairs of collections are always kept.
 */
public class EdgeLabelFilter {

    // Assign the reviewed labels to keep, by edge collection (subject collection, then object collection). Note that
    // PART_OF is kept from cell type to anatomy, but not from anatomy to cell type.
    static final Map<String, Set<String>> KEPT_LABELS_BY_PAIR = Map.of("CL-UBERON",
            Set.of("PART_OF",
                    "HAS_SOMA_LOCATION",
                    "HAS_SYNAPTIC_IO_IN_REGION",
                    "AXON_SYNAPSES_IN",
                    "HAS_DENDRITE_LOCATION",
                    "HAS_PRESYNAPTIC_TERMINAL_IN",
                    "HAS_SENSORY_DENDRITE_IN",
                    "HAS_SYNAPTIC_TERMINAL_IN"),
            "UBERON-CL",
            Set.of("HAS_PART", "COMPOSED_PRIMARILY_OF"));

    // Count dropped edges by edge collection, then by label
    private final Map<String, Map<String, Long>> dropped = new TreeMap<>();

    /**
     * Whether to keep an edge with the given label between the given collections. Records the edge if it is dropped.
     *
     * @param idPair Edge collection name, the subject and object collections joined by a hyphen (e.g. "CL-UBERON")
     * @param label  Normalized edge label (e.g. "PART_OF")
     * @return {@code true} if the edge is to be kept
     */
    public boolean keep(String idPair, String label) {
        Set<String> kept = KEPT_LABELS_BY_PAIR.get(idPair);
        if (kept == null || kept.contains(label)) {
            return true;
        }
        dropped.computeIfAbsent(idPair, k -> new TreeMap<>()).merge(label, 1L, Long::sum);
        return false;
    }

    /**
     * Get the number of edges dropped so far.
     *
     * @param idPair Edge collection name
     * @return Number of dropped edges, over all labels
     */
    public long getDroppedCount(String idPair) {
        return dropped.getOrDefault(idPair, Map.of()).values().stream().mapToLong(Long::longValue).sum();
    }

    /**
     * Get the number of edges dropped so far, by label.
     *
     * @param idPair Edge collection name
     * @return Dropped edge counts by label
     */
    public Map<String, Long> getDroppedCounts(String idPair) {
        return Map.copyOf(dropped.getOrDefault(idPair, Map.of()));
    }

    /**
     * Summarize the edges dropped so far, one line for each edge collection, most frequent label first.
     *
     * @return Summary lines; empty if no edges have been dropped
     */
    public List<String> summarize() {
        List<String> lines = new ArrayList<>();
        for (Map.Entry<String, Map<String, Long>> pair : dropped.entrySet()) {
            List<String> labelCounts = sortedLabelCounts(pair.getValue())
                    .entrySet()
                    .stream()
                    .map(e -> e.getKey() + "=" + e.getValue())
                    .toList();
            lines.add("Dropped " + getDroppedCount(pair.getKey()) + " edges from " + pair.getKey() + ": " + String.join(
                    ", ",
                    labelCounts));
        }
        return lines;
    }

    /**
     * Log one {@code edges_dropped_by_label_filter} event per id pair that had edges dropped, with the count and the
     * dropped labels, most frequent first.
     *
     * @param log The logger to write to
     */
    public void logSummary(Logger log) {
        for (Map.Entry<String, Map<String, Long>> pair : dropped.entrySet()) {
            Events.warn(log, "edges_dropped_by_label_filter", "edge_collection", pair.getKey(),
                    "records_rejected", getDroppedCount(pair.getKey()),
                    "label_counts", sortedLabelCounts(pair.getValue()));
        }
    }

    /** Label counts, most frequent first, then by label. */
    private static Map<String, Long> sortedLabelCounts(Map<String, Long> counts) {
        Map<String, Long> sorted = new LinkedHashMap<>();
        counts.entrySet()
                .stream()
                .sorted(Map.Entry.<String, Long>comparingByValue().reversed().thenComparing(Map.Entry.comparingByKey()))
                .forEach(e -> sorted.put(e.getKey(), e.getValue()));
        return sorted;
    }
}
