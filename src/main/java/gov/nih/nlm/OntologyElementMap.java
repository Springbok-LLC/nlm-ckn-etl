package gov.nih.nlm;

import java.net.URI;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/**
 * Contains ontology terms for all elements with a non-empty "about" attribute
 * and at least one "label" element.
 */
public class OntologyElementMap {

    private String title;
    private String description;
    private URI purl;
    private URI versionIRI;
    private URI root;
    private final Set<String> ids;
    private final Map<String, OntologyTerm> terms;
    private final Map<String, Integer> skipped = new java.util.TreeMap<>();

    /** Count an element the parser passed over, by reason, for the per-file log event. */
    public void skip(String reason) {
        skipped.merge(reason, 1, Integer::sum);
    }

    public Map<String, Integer> getSkipped() {
        return skipped;
    }

    public OntologyElementMap() {
        ids = new HashSet<>();
        terms = new HashMap<>();
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getDescription() {
        return description;
    }

    public void setDescription(String description) {
        this.description = description;
    }

    public URI getPurl() {
        return purl;
    }

    public void setPurl(URI purl) {
        this.purl = purl;
    }

    public URI getVersionIRI() {
        return versionIRI;
    }

    public void setVersionIRI(URI versionIRI) {
        this.versionIRI = versionIRI;
    }

    public URI getRoot() {
        return root;
    }

    public void setRoot(URI root) {
        this.root = root;
    }

    public Set<String> getIds() {
        return ids;
    }

    public Map<String, OntologyTerm> getTerms() {
        return terms;
    }

    /**
     * Contains ontology term PURL and label
     *
     * @param purl  Ontology term PURL
     * @param label Ontology term label
     */
    public record OntologyTerm(URI purl, String label) {
    }
}
