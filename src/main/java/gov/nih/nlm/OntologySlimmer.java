package gov.nih.nlm;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.xml.namespace.QName;
import javax.xml.stream.XMLEventReader;
import javax.xml.stream.XMLEventWriter;
import javax.xml.stream.XMLInputFactory;
import javax.xml.stream.XMLOutputFactory;
import javax.xml.stream.XMLStreamException;
import javax.xml.stream.events.StartElement;
import javax.xml.stream.events.XMLEvent;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static gov.nih.nlm.PathUtilities.OBO_DIR;

/**
 * Filters large OWL ontology files by taxon using StAX streaming, so the files can be processed with minimal memory.
 *
 * <p>Two complementary filters are provided, each restricting to the taxa in {@link TaxonConfig#PERMITTED_TAXA}:
 * <ul>
 *   <li><b>Inclusion</b> ({@link #slimOntologyByTaxonInclusion}) — retain only classes positively asserted to be in a
 *   permitted taxon via an {@code owl:someValuesFrom} restriction. Correct for ontologies whose classes are
 *   species-specific by design (e.g. the Protein Ontology, where every class is {@code only_in_taxon} some organism).</li>
 *   <li><b>Exclusion</b> ({@link #slimOntologyByTaxonExclusion}) — retain every class except those whose taxon
 *   constraints rule out every permitted taxon. Correct for ontologies that are predominantly pan-taxonomic (e.g.
 *   Uberon, where most anatomical classes carry no taxon constraint and apply across species).</li>
 * </ul>
 */
public class OntologySlimmer {

    private static final Logger LOG = LoggerFactory.getLogger(OntologySlimmer.class);

    private static final String OWL_NS = "http://www.w3.org/2002/07/owl#";
    private static final String RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
    private static final String RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#";
    private static final String OBO_NS = "http://purl.obolibrary.org/obo/";

    private static final String NCBITAXON_PREFIX = OBO_NS + "NCBITaxon_";

    // RO taxon-constraint properties (used both as restriction onProperty URIs and as direct annotation local names).
    // never_in_taxon is an exclusion; only_in_taxon and in_taxon are positive assertions of taxon membership.
    private static final String NEVER_IN_TAXON_URI = OBO_NS + "RO_0002161"; // never_in_taxon
    private static final String ONLY_IN_TAXON_URI = OBO_NS + "RO_0002160";  // only_in_taxon
    private static final String IN_TAXON_URI = OBO_NS + "RO_0002162";       // in_taxon
    private static final String NEVER_IN_TAXON_LOCAL = "RO_0002161";
    private static final String ONLY_IN_TAXON_LOCAL = "RO_0002160";
    private static final String IN_TAXON_LOCAL = "RO_0002162";

    /**
     * Decides whether a fully-buffered top-level {@code owl:Class} should be written to the output.
     */
    @FunctionalInterface
    private interface ClassFilter {
        boolean keep(List<XMLEvent> classEvents);
    }

    /**
     * Create an {@link XMLInputFactory} hardened against XXE by disabling DTDs and external entities. The ontology
     * inputs do not use DTDs or entity references, so disabling them is safe and prevents external-entity attacks.
     *
     * @return A hardened input factory
     */
    private static XMLInputFactory hardenedInputFactory() {
        XMLInputFactory factory = XMLInputFactory.newInstance();
        factory.setProperty(XMLInputFactory.SUPPORT_DTD, false);
        factory.setProperty(XMLInputFactory.IS_SUPPORTING_EXTERNAL_ENTITIES, false);
        return factory;
    }

    /**
     * Filter an OWL file to retain only classes with a taxon restriction for the given NCBI taxon ID. Copies the
     * ontology header, all annotation and object property declarations, and only those {@code owl:Class} elements that
     * contain an {@code owl:someValuesFrom} restriction referencing the specified taxon. Drops all {@code owl:Axiom}
     * elements.
     *
     * @param inputFile  Path to the full OWL file
     * @param outputFile Path to write the filtered OWL file
     * @param taxonIds   NCBI taxon IDs to filter on (e.g., {@code {"9606"}} for human); a class is retained when it is
     *                   asserted to be in any of them
     * @return The number of classes written
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    public static int slimOntologyByTaxonInclusion(Path inputFile, Path outputFile, Set<String> taxonIds) throws IOException, XMLStreamException {
        Set<String> taxonURIs = new HashSet<>();
        for (String taxonId : taxonIds) {
            taxonURIs.add(NCBITAXON_PREFIX + taxonId);
        }
        return filterClasses(inputFile, outputFile, "inclusion", events -> hasSomeValuesFrom(events, taxonURIs));
    }

    /**
     * Filter an OWL file to retain every class except those whose taxon constraints exclude the target taxon. Copies the
     * ontology header, all annotation and object property declarations, and every {@code owl:Class} element that is not
     * excluded. Drops all {@code owl:Axiom} elements.
     *
     * <p>A class is excluded for a single target taxon when it asserts either:
     * <ul>
     *   <li>{@code never_in_taxon} (RO:0002161) {@code T} where the target taxon is {@code T} or a descendant of
     *   {@code T} (i.e. {@code T} is in the target's lineage); or</li>
     *   <li>a positive taxon restriction — {@code only_in_taxon} (RO:0002160) or {@code in_taxon} (RO:0002162) —
     *   restricted to taxa none of which cover the target taxon.</li>
     * </ul>
     * Both the direct annotation form (e.g. {@code <obo:RO_0002161 rdf:resource="NCBITaxon_..."/>}) and the logical
     * restriction form ({@code owl:onProperty} + {@code owl:someValuesFrom}) are recognized.
     *
     * <p>When more than one taxon is permitted, a class is written if it is <em>not</em> excluded for at least one of
     * them (i.e. it is relevant to some permitted taxon), and dropped only when excluded for every permitted taxon.
     *
     * @param inputFile      Path to the full OWL file
     * @param outputFile     Path to write the filtered OWL file
     * @param targetLineages One lineage per permitted taxon, each the taxon together with all of its ancestors (see
     *                       {@link #buildTaxonLineages})
     * @return The number of classes written
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    public static int slimOntologyByTaxonExclusion(Path inputFile, Path outputFile, List<Set<String>> targetLineages) throws IOException, XMLStreamException {
        return filterClasses(inputFile, outputFile, "exclusion", events -> {
            TaxonConstraints constraints = extractTaxonConstraints(events);
            for (Set<String> lineage : targetLineages) {
                if (!isExcludedForLineage(constraints, lineage)) {
                    return true; // relevant to at least one permitted taxon: keep
                }
            }
            return false; // excluded for every permitted taxon: drop
        });
    }

    /**
     * Stream an OWL file, passing through the header and all top-level non-class, non-axiom content, buffering each
     * top-level {@code owl:Class} and writing it only when the supplied filter keeps it. All {@code owl:Axiom} elements
     * are dropped.
     *
     * @param inputFile  Path to the full OWL file
     * @param outputFile Path to write the filtered OWL file
     * @param mode       {@code inclusion} or {@code exclusion}, for the log line
     * @param filter     Decides whether each buffered class is written
     * @return The number of classes written
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    private static int filterClasses(Path inputFile, Path outputFile, String mode, ClassFilter filter) throws IOException, XMLStreamException {
        XMLInputFactory inputFactory = hardenedInputFactory();
        XMLOutputFactory outputFactory = XMLOutputFactory.newInstance();
        long started = System.nanoTime();
        int classesWritten = 0;
        int classesSkipped = 0;

        try (var fis = new BufferedInputStream(new FileInputStream(inputFile.toFile()));
             var fos = new BufferedOutputStream(new FileOutputStream(outputFile.toFile()))) {

            XMLEventReader reader = inputFactory.createXMLEventReader(fis);
            XMLEventWriter writer = outputFactory.createXMLEventWriter(fos, "UTF-8");

            // State machine: buffer each owl:Class, decide keep/discard at its end tag
            List<XMLEvent> buffer = new ArrayList<>();
            boolean inClass = false;   // inside a top-level owl:Class (has rdf:about)
            boolean inAxiom = false;   // inside an owl:Axiom (always discarded)
            int depth = 0;             // XML nesting depth
            int elementStartDepth = 0; // depth when the current class/axiom opened

            while (reader.hasNext()) {
                XMLEvent event = reader.nextEvent();

                if (event.isStartElement()) {
                    depth++;
                    String localName = event.asStartElement().getName().getLocalPart();
                    String nsURI = event.asStartElement().getName().getNamespaceURI();

                    // Start buffering a top-level owl:Class; we decide keep/discard at its end tag. A named class may be
                    // serialized with either rdf:about or rdf:ID; accept both so neither bypasses the filter.
                    if (nsURI.equals(OWL_NS) && localName.equals("Class") && !inClass && !inAxiom) {
                        var aboutAttr = event.asStartElement().getAttributeByName(new QName(RDF_NS, "about"));
                        var idAttr = event.asStartElement().getAttributeByName(new QName(RDF_NS, "ID"));
                        if (aboutAttr != null || idAttr != null) {
                            inClass = true;
                            elementStartDepth = depth;
                            buffer.clear();
                            buffer.add(event);
                            continue;
                        }
                    }

                    // Skip owl:Axiom elements entirely
                    if (nsURI.equals(OWL_NS) && localName.equals("Axiom") && !inClass) {
                        inAxiom = true;
                        elementStartDepth = depth;
                        continue;
                    }

                    if (inClass) {
                        buffer.add(event);
                        continue;
                    }

                    if (inAxiom) {
                        continue;
                    }

                    // Top-level non-class, non-axiom elements pass through (header, properties, etc.)
                    writer.add(event);

                } else if (event.isEndElement()) {
                    if (inClass) {
                        buffer.add(event);
                        // Class fully parsed: flush buffer if the filter keeps it, otherwise discard
                        if (depth == elementStartDepth) {
                            inClass = false;
                            if (filter.keep(buffer)) {
                                for (XMLEvent e : buffer) {
                                    writer.add(e);
                                }
                                classesWritten++;
                            } else {
                                classesSkipped++;
                            }
                            buffer.clear();
                        }
                    } else if (inAxiom) {
                        if (depth == elementStartDepth) {
                            inAxiom = false;
                        }
                    } else {
                        writer.add(event);
                    }
                    depth--;

                } else {
                    // Characters, comments, processing instructions, etc.
                    if (inClass) {
                        buffer.add(event);
                    } else if (!inAxiom) {
                        writer.add(event);
                    }
                }
            }

            writer.flush();
            reader.close();
        }

        Events.info(LOG, "ontology_slimmed", "input_file", inputFile.toString(), "output_file", outputFile.toString(),
                "mode", mode, "records_out", classesWritten, "records_rejected", classesSkipped,
                "duration_ms", RunLog.elapsedMs(started));
        if (classesWritten == 0) {
            Events.warn(LOG, "ontology_slim_empty", "input_file", inputFile.toString(),
                    "output_file", outputFile.toString(), "mode", mode);
        }
        return classesWritten;
    }

    /**
     * Whether the buffered class contains an {@code owl:someValuesFrom} restriction referencing any of the target URIs.
     *
     * @param classEvents Buffered events for a single class
     * @param targetURIs  The taxon URIs to match
     * @return {@code true} if a matching restriction is present
     */
    private static boolean hasSomeValuesFrom(List<XMLEvent> classEvents, Set<String> targetURIs) {
        for (XMLEvent e : classEvents) {
            if (e.isStartElement()) {
                StartElement se = e.asStartElement();
                if (OWL_NS.equals(se.getName().getNamespaceURI())
                        && "someValuesFrom".equals(se.getName().getLocalPart())
                        && targetURIs.contains(getResource(se))) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * The taxon constraints asserted by a single class, split by polarity: {@code neverInTaxon} taxa exclude the class
     * from a taxon and its descendants, while {@code positiveInTaxon} taxa (from {@code only_in_taxon} and
     * {@code in_taxon}) assert the class is found only within the named taxa (and their descendants).
     *
     * @param neverInTaxon    Taxa named by {@code never_in_taxon} constraints
     * @param positiveInTaxon Taxa named by {@code only_in_taxon} or {@code in_taxon} constraints
     */
    record TaxonConstraints(Set<String> neverInTaxon, Set<String> positiveInTaxon) {
    }

    /**
     * Extract the taxon constraints asserted by a buffered class. Recognizes {@code never_in_taxon} (RO:0002161),
     * {@code only_in_taxon} (RO:0002160), and {@code in_taxon} (RO:0002162) in both the direct annotation form and the
     * logical {@code owl:onProperty}/{@code owl:someValuesFrom} restriction form.
     *
     * @param classEvents Buffered events for a single class
     * @return The taxon constraints the class asserts
     */
    static TaxonConstraints extractTaxonConstraints(List<XMLEvent> classEvents) {
        Set<String> neverInTaxon = new HashSet<>();
        Set<String> positiveInTaxon = new HashSet<>();
        String restrictionProperty = null; // RO property of the enclosing owl:Restriction, if relevant

        for (XMLEvent e : classEvents) {
            if (e.isStartElement()) {
                StartElement se = e.asStartElement();
                String ns = se.getName().getNamespaceURI();
                String ln = se.getName().getLocalPart();

                if (OWL_NS.equals(ns) && "Restriction".equals(ln)) {
                    restrictionProperty = null;
                } else if (OWL_NS.equals(ns) && "onProperty".equals(ln)) {
                    String p = getResource(se);
                    if (NEVER_IN_TAXON_URI.equals(p) || ONLY_IN_TAXON_URI.equals(p) || IN_TAXON_URI.equals(p)) {
                        restrictionProperty = p;
                    }
                } else if (OWL_NS.equals(ns) && "someValuesFrom".equals(ln)) {
                    String t = getResource(se);
                    if (t != null && restrictionProperty != null) {
                        (NEVER_IN_TAXON_URI.equals(restrictionProperty) ? neverInTaxon : positiveInTaxon).add(t);
                    }
                } else if (OBO_NS.equals(ns) && NEVER_IN_TAXON_LOCAL.equals(ln)) {
                    String t = getResource(se);
                    if (t != null) {
                        neverInTaxon.add(t);
                    }
                } else if (OBO_NS.equals(ns) && (ONLY_IN_TAXON_LOCAL.equals(ln) || IN_TAXON_LOCAL.equals(ln))) {
                    String t = getResource(se);
                    if (t != null) {
                        positiveInTaxon.add(t);
                    }
                }
            } else if (e.isEndElement()) {
                if (OWL_NS.equals(e.asEndElement().getName().getNamespaceURI())
                        && "Restriction".equals(e.asEndElement().getName().getLocalPart())) {
                    restrictionProperty = null;
                }
            }
        }

        return new TaxonConstraints(neverInTaxon, positiveInTaxon);
    }

    /**
     * Whether a class with the given taxon constraints is excluded for a target taxon, described by its lineage (the
     * taxon and all of its ancestors).
     *
     * @param constraints   The taxon constraints asserted by the class
     * @param targetLineage The target taxon together with all of its ancestors
     * @return {@code true} if the class should be excluded for that target taxon
     */
    static boolean isExcludedForLineage(TaxonConstraints constraints, Set<String> targetLineage) {
        // never_in_taxon T excludes the target when the target is T or a descendant of T (T in the target lineage).
        for (String t : constraints.neverInTaxon()) {
            if (targetLineage.contains(t)) {
                return true;
            }
        }
        // A positive taxon restriction excludes the target when none of the named taxa cover it.
        if (!constraints.positiveInTaxon().isEmpty()) {
            for (String t : constraints.positiveInTaxon()) {
                if (targetLineage.contains(t)) {
                    return false;
                }
            }
            return true;
        }
        return false;
    }

    /**
     * Build the set containing the given taxon and all of its ancestors by walking the {@code rdfs:subClassOf} chain in
     * an NCBITaxon ontology file (e.g. taxslim.owl). The returned set is the taxon for which a {@code never_in_taxon}
     * constraint excludes a class, and against which {@code only_in_taxon} membership is tested.
     *
     * @param taxslimFile Path to an NCBITaxon OWL file containing the taxon hierarchy
     * @param taxonId     NCBI taxon ID whose lineage to build (e.g., "9606" for human)
     * @return The taxon URI together with all ancestor taxon URIs
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    public static Set<String> buildTaxonLineage(Path taxslimFile, String taxonId) throws IOException, XMLStreamException {
        return walkLineage(readChildToParents(taxslimFile), taxonId);
    }

    /**
     * Build one lineage per taxon (see {@link #buildTaxonLineage}), reading the NCBITaxon hierarchy from
     * {@code taxslimFile} once and walking it from each taxon.
     *
     * @param taxslimFile Path to an NCBITaxon OWL file containing the taxon hierarchy
     * @param taxonIds    NCBI taxon IDs whose lineages to build (e.g., {@code {"9606"}} for human)
     * @return One lineage (taxon plus all ancestors) per taxon id
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    public static List<Set<String>> buildTaxonLineages(Path taxslimFile, Set<String> taxonIds) throws IOException, XMLStreamException {
        Map<String, Set<String>> childToParents = readChildToParents(taxslimFile);
        List<Set<String>> lineages = new ArrayList<>();
        for (String taxonId : taxonIds) {
            lineages.add(walkLineage(childToParents, taxonId));
        }
        return lineages;
    }

    /**
     * Read the child → parents map from the {@code rdfs:subClassOf} edges among NCBITaxon classes in an NCBITaxon
     * ontology file (e.g. taxslim.owl).
     *
     * @param taxslimFile Path to an NCBITaxon OWL file containing the taxon hierarchy
     * @return A map from each NCBITaxon URI to the set of its parent NCBITaxon URIs
     * @throws IOException        if an I/O error occurs
     * @throws XMLStreamException if an XML parsing error occurs
     */
    private static Map<String, Set<String>> readChildToParents(Path taxslimFile) throws IOException, XMLStreamException {
        Map<String, Set<String>> childToParents = new HashMap<>();
        XMLInputFactory inputFactory = hardenedInputFactory();

        try (var fis = new BufferedInputStream(new FileInputStream(taxslimFile.toFile()))) {
            XMLEventReader reader = inputFactory.createXMLEventReader(fis);
            String currentTaxon = null; // rdf:about of the NCBITaxon class currently being read
            int classDepth = -1;
            int depth = 0;

            while (reader.hasNext()) {
                XMLEvent event = reader.nextEvent();
                if (event.isStartElement()) {
                    depth++;
                    StartElement se = event.asStartElement();
                    String ns = se.getName().getNamespaceURI();
                    String ln = se.getName().getLocalPart();

                    if (OWL_NS.equals(ns) && "Class".equals(ln) && currentTaxon == null) {
                        var about = se.getAttributeByName(new QName(RDF_NS, "about"));
                        if (about != null && about.getValue().startsWith(NCBITAXON_PREFIX)) {
                            currentTaxon = about.getValue();
                            classDepth = depth;
                        }
                    } else if (currentTaxon != null && RDFS_NS.equals(ns) && "subClassOf".equals(ln)) {
                        String parent = getResource(se);
                        if (parent != null && parent.startsWith(NCBITAXON_PREFIX)) {
                            childToParents.computeIfAbsent(currentTaxon, k -> new HashSet<>()).add(parent);
                        }
                    }
                } else if (event.isEndElement()) {
                    if (currentTaxon != null && depth == classDepth) {
                        currentTaxon = null;
                        classDepth = -1;
                    }
                    depth--;
                }
            }
            reader.close();
        }
        return childToParents;
    }

    /**
     * Walk up from the target taxon, collecting it and all of its ancestors.
     *
     * @param childToParents A map from each NCBITaxon URI to its parent NCBITaxon URIs
     * @param taxonId        NCBI taxon ID whose lineage to build
     * @return The taxon URI together with all ancestor taxon URIs
     */
    private static Set<String> walkLineage(Map<String, Set<String>> childToParents, String taxonId) {
        Set<String> lineage = new HashSet<>();
        Deque<String> stack = new ArrayDeque<>();
        stack.push(NCBITAXON_PREFIX + taxonId);
        while (!stack.isEmpty()) {
            String taxon = stack.pop();
            if (lineage.add(taxon)) {
                for (String parent : childToParents.getOrDefault(taxon, Set.of())) {
                    stack.push(parent);
                }
            }
        }
        return lineage;
    }

    /**
     * Read the value of the {@code rdf:resource} attribute of a start element, or {@code null} if absent.
     *
     * @param se Start element
     * @return The {@code rdf:resource} value, or {@code null}
     */
    private static String getResource(StartElement se) {
        var attr = se.getAttributeByName(new QName(RDF_NS, "resource"));
        return attr == null ? null : attr.getValue();
    }

    /**
     * Slim downloaded ontology files in data/obo before graph loading:
     * <ul>
     *   <li>{@code pr.owl} → {@code pr-slim.owl}: retain only human (NCBITaxon 9606) protein classes (inclusion).</li>
     *   <li>{@code uberon-base.owl} → {@code uberon-human.owl}: retain all anatomy except classes a taxon constraint
     *   excludes from human (exclusion), using the NCBITaxon hierarchy in {@code taxslim.owl}.</li>
     * </ul>
     * Each slimmed input is moved to {@code .archive/} after a successful slim.
     *
     * @param args (None expected)
     */
    public static void main(String[] args) {
        RunLog.run("OntologySlimmer", OntologySlimmer::slimDownloads,
                "obo_dir", OBO_DIR.toString(), "permitted_taxa", String.join(",", new java.util.TreeSet<>(TaxonConfig.PERMITTED_TAXA)));
    }

    private static void slimDownloads() throws IOException, XMLStreamException {
        Path archiveDir = OBO_DIR.resolve(".archive");

        // Inclusion slim: pr.owl -> pr-slim.owl
        Path prFull = OBO_DIR.resolve("pr.owl");
        Path prSlim = OBO_DIR.resolve("pr-slim.owl");
        if (!Files.exists(prFull)) {
            throw new IllegalStateException("pr.owl not found in " + OBO_DIR);
        }
        slimOntologyByTaxonInclusion(prFull, prSlim, TaxonConfig.PERMITTED_TAXA);
        archive(prFull, archiveDir.resolve("pr.owl"), archiveDir);

        // Exclusion slim: uberon-base.owl -> uberon-human.owl, using the NCBITaxon hierarchy in taxslim.owl
        Path taxslimFile = OBO_DIR.resolve("taxslim.owl");
        Path uberonFull = OBO_DIR.resolve("uberon-base.owl");
        Path uberonSlim = OBO_DIR.resolve("uberon-human.owl");
        if (!Files.exists(uberonFull)) {
            throw new IllegalStateException("uberon-base.owl not found in " + OBO_DIR);
        }
        if (!Files.exists(taxslimFile)) {
            throw new IllegalStateException("taxslim.owl not found in " + OBO_DIR);
        }
        List<Set<String>> lineages = buildTaxonLineages(taxslimFile, TaxonConfig.PERMITTED_TAXA);
        int lineageTaxa = lineages.stream().mapToInt(Set::size).sum();
        Events.info(LOG, "taxon_lineages_built", "taxa_count", lineageTaxa,
                "permitted_taxa_count", lineages.size());
        slimOntologyByTaxonExclusion(uberonFull, uberonSlim, lineages);
        archive(uberonFull, archiveDir.resolve("uberon-base.owl"), archiveDir);
    }

    /** Moves a slimmed input into the archive, replacing any earlier archived copy. */
    private static void archive(Path from, Path to, Path archiveDir) throws IOException {
        Files.createDirectories(archiveDir);
        Files.move(from, to, StandardCopyOption.REPLACE_EXISTING);
        Events.info(LOG, "ontology_file_archived", "from", from.toString(), "to", to.toString());
    }
}
