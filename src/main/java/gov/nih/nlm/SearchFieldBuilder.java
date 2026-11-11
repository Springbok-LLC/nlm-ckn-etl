package gov.nih.nlm;

import java.util.List;
import java.util.Set;

/**
 * Builds the {@code _search} field of a vertex: the identifier forms by which the vertex can be searched.
 */
public class SearchFieldBuilder {

    // Collections of OBO Foundry ontologies, whose terms have an OBO PURL
    static final Set<String> OBO_COLLECTIONS = Set.of("CHEBI",
            "CL",
            "GO",
            "HP",
            "HsapDv",
            "MONDO",
            "NCBITaxon",
            "PATO",
            "PR",
            "UBERON");

    // Assign the historically correct OBO PURL prefix
    static final String OBO_PURL_PREFIX = "http://purl.obolibrary.org/obo/";

    // The same PURL over https, the form browsers and shared links use; purl.obolibrary.org serves both
    static final String OBO_PURL_HTTPS_PREFIX = "https://purl.obolibrary.org/obo/";

    /**
     * Build the {@code _search} values for a vertex. The collection case is preserved; matching without regard to case
     * is left to the analyzer.
     * <p>
     * OBO ontology terms are searchable by CURIE, underscore form, and PURL over http or https, e.g.
     * {@code UBERON:0002405}, {@code UBERON_0002405}, {@code http://purl.obolibrary.org/obo/UBERON_0002405}, and
     * {@code https://purl.obolibrary.org/obo/UBERON_0002405}. All other vertices are
     * searchable by the colon and underscore forms of the collection and key, as displayed in the UI.
     *
     * @param collection Vertex collection name, e.g. "UBERON"
     * @param key        Vertex key, e.g. "0002405"
     * @return Values for the {@code _search} field
     */
    public static List<String> build(String collection, String key) {
        String curie = collection + ":" + key;
        String underscored = collection + "_" + key;
        if (OBO_COLLECTIONS.contains(collection)) {
            return List.of(curie, underscored, OBO_PURL_PREFIX + underscored, OBO_PURL_HTTPS_PREFIX + underscored);
        }
        return List.of(curie, underscored);
    }
}
