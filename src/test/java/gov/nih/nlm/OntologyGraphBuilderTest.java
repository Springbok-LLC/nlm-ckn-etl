package gov.nih.nlm;

import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoVertexCollection;
import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import org.apache.jena.graph.NodeFactory;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OntologyGraphBuilderTest {

    // Assign location of test ontology files
    private static final Path USR_DIR = Paths.get(System.getProperty("user.dir"));
    private static final Path OBO_DIR = USR_DIR.resolve("src/test/data/obo");

    // --- insertVertices / insertEdges counting tests (in-memory collections, no ArangoDB needed) ---

    /** A vertex collection holding the given keys; inserting or updating a key in failKeys throws. */
    private static ArangoVertexCollection vertexCollection(Set<String> existing, Set<String> failKeys) {
        return (ArangoVertexCollection) Proxy.newProxyInstance(
            OntologyGraphBuilderTest.class.getClassLoader(), new Class<?>[]{ArangoVertexCollection.class},
            (proxy, method, args) -> {
                switch (method.getName()) {
                    case "getVertex":
                        return existing.contains((String) args[0]) ? new BaseDocument() : null;
                    case "insertVertex":
                        if (failKeys.contains(((BaseDocument) args[0]).getKey())) {
                            throw new RuntimeException("insert refused");
                        }
                        return null;
                    case "updateVertex":
                        if (failKeys.contains((String) args[0])) {
                            throw new RuntimeException("update refused");
                        }
                        return null;
                    default:
                        throw new UnsupportedOperationException(method.getName());
                }
            });
    }

    private static ArangoEdgeCollection edgeCollection(Set<String> existing, Set<String> failKeys) {
        return (ArangoEdgeCollection) Proxy.newProxyInstance(
            OntologyGraphBuilderTest.class.getClassLoader(), new Class<?>[]{ArangoEdgeCollection.class},
            (proxy, method, args) -> {
                switch (method.getName()) {
                    case "getEdge":
                        return existing.contains((String) args[0]) ? new BaseEdgeDocument() : null;
                    case "insertEdge":
                        if (failKeys.contains(((BaseEdgeDocument) args[0]).getKey())) {
                            throw new RuntimeException("insert refused");
                        }
                        return null;
                    case "updateEdge":
                        if (failKeys.contains((String) args[0])) {
                            throw new RuntimeException("update refused");
                        }
                        return null;
                    default:
                        throw new UnsupportedOperationException(method.getName());
                }
            });
    }

    private static BaseDocument vertex(String key, Object deprecated) {
        BaseDocument doc = new BaseDocument();
        doc.setKey(key);
        doc.addAttribute("label", key);
        if (deprecated != null) {
            doc.addAttribute("deprecated", deprecated);
        }
        return doc;
    }

    private static Map<String, Map<String, BaseDocument>> vertexDocs(BaseDocument... docs) {
        Map<String, BaseDocument> byNumber = new LinkedHashMap<>();
        for (BaseDocument doc : docs) {
            byNumber.put(doc.getKey(), doc);
        }
        return Map.of("CL", byNumber);
    }

    @Test
    void insertVertices_countsInsertedUpdatedAndSkipped(@TempDir Path dir) throws IOException {
        var collections = Map.of("CL", vertexCollection(Set.of("0000002"), Set.of()));
        var docs = vertexDocs(vertex("0000001", null), vertex("0000002", null), vertex("0000003", "true"));

        var summary = OntologyGraphBuilder.insertVertices(collections, docs, dir.resolve("deprecated.txt"));

        assertEquals(new OntologyGraphBuilder.InsertSummary(3, 1, 1, 1, 0), summary);
        assertEquals("CL_0000003\n", Files.readString(dir.resolve("deprecated.txt")));
    }

    @Test
    void insertVertices_throwsAfterAttemptingAllWhenAnyFail(@TempDir Path dir) {
        var collections = Map.of("CL", vertexCollection(Set.of(), Set.of("0000001")));
        var docs = vertexDocs(vertex("0000001", null), vertex("0000002", null));

        var e = assertThrows(IllegalStateException.class,
            () -> OntologyGraphBuilder.insertVertices(collections, docs, dir.resolve("deprecated.txt")));

        assertTrue(e.getMessage().contains("1 of 2 vertices"));
    }

    private static BaseEdgeDocument edge(String key, String from, String to) {
        BaseEdgeDocument doc = new BaseEdgeDocument();
        doc.setKey(key);
        doc.setFrom(from);
        doc.setTo(to);
        return doc;
    }

    private static Map<String, Map<String, BaseEdgeDocument>> edgeDocs(BaseEdgeDocument... docs) {
        Map<String, BaseEdgeDocument> byKey = new LinkedHashMap<>();
        for (BaseEdgeDocument doc : docs) {
            byKey.put(doc.getKey(), doc);
        }
        return Map.of("CL-CL", byKey);
    }

    @Test
    void insertEdges_countsInsertedUpdatedAndSkippedForMissingEndpoint() {
        // CL/1 and CL/2 exist as vertices; CL/9 does not (for example a deprecated term that was not inserted)
        var vertices = Map.of("CL", vertexCollection(Set.of("1", "2"), Set.of()));
        var edges = Map.of("CL-CL", edgeCollection(Set.of("e2"), Set.of()));
        var docs = edgeDocs(edge("e1", "CL/1", "CL/2"), edge("e2", "CL/1", "CL/2"), edge("e3", "CL/1", "CL/9"));

        var summary = OntologyGraphBuilder.insertEdges(vertices, edges, docs);

        assertEquals(new OntologyGraphBuilder.InsertSummary(3, 1, 1, 1, 0), summary);
    }

    @Test
    void insertEdges_throwsAfterAttemptingAllWhenAnyFail() {
        var vertices = Map.of("CL", vertexCollection(Set.of("1", "2"), Set.of()));
        var edges = Map.of("CL-CL", edgeCollection(Set.of(), Set.of("e1")));
        var docs = edgeDocs(edge("e1", "CL/1", "CL/2"), edge("e2", "CL/2", "CL/1"));

        var e = assertThrows(IllegalStateException.class,
            () -> OntologyGraphBuilder.insertEdges(vertices, edges, docs));

        assertTrue(e.getMessage().contains("1 of 2 edges"));
    }

    // --- createVTuple tests (no ArangoDB needed) ---

    @Test
    void createVTuple_validCLTerm() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/CL_0000235");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("CL_0000235", vtuple.term());
        assertEquals("CL", vtuple.id());
        assertEquals("0000235", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_validGOTerm() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/GO_0031268");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("GO_0031268", vtuple.term());
        assertEquals("GO", vtuple.id());
        assertEquals("0031268", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_validUBERONTerm() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/UBERON_0000061");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("UBERON_0000061", vtuple.term());
        assertEquals("UBERON", vtuple.id());
        assertEquals("0000061", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_invalidPrefix() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/BFO_0000002");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("BFO_0000002", vtuple.term());
        assertEquals("BFO", vtuple.id());
        assertEquals("0000002", vtuple.number());
        assertFalse(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_nonUriNode() {
        var node = NodeFactory.createLiteralString("not a URI");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertNull(vtuple.term());
        assertNull(vtuple.id());
        assertNull(vtuple.number());
        assertFalse(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_uriWithFragment() {
        var node = NodeFactory.createURI("http://www.w3.org/2000/01/rdf-schema#subClassOf");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        // "subClassOf" has no underscore or colon separator, so tokens will be null
        assertNull(vtuple.term());
        assertFalse(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_ncbiTaxon() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/NCBITaxon_9606");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("NCBITaxon_9606", vtuple.term());
        assertEquals("NCBITaxon", vtuple.id());
        assertEquals("9606", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_nonHumanTaxonIsNotValidVertex() {
        // The graph is restricted to the permitted taxa, so a non-human taxon (e.g. Proboscidea/elephants,
        // referenced by an Uberon in_taxon constraint) must not be a valid vertex.
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/NCBITaxon_9779");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("NCBITaxon_9779", vtuple.term());
        assertEquals("NCBITaxon", vtuple.id());
        assertEquals("9779", vtuple.number());
        assertFalse(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_validHPTerm() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/HP_0000001");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("HP_0000001", vtuple.term());
        assertEquals("HP", vtuple.id());
        assertEquals("0000001", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_validMONDOTerm() {
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/MONDO_0000001");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("MONDO_0000001", vtuple.term());
        assertEquals("MONDO", vtuple.id());
        assertEquals("0000001", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_validCellSetDatasetSourceKey() {
        // A single-organ dataset keeps the bare source dataset_version_id (one
        // underscore after the CSD prefix; the id itself carries hyphens).
        var node = NodeFactory.createURI(
                "http://purl.obolibrary.org/obo/CSD_2b1f9ac3-1234-5678-9abc-def012345678");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("CSD", vtuple.id());
        assertEquals("2b1f9ac3-1234-5678-9abc-def012345678", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_validCellSetDatasetCompositeKey() {
        // A dataset filtered for an organ is keyed "<dvid>__<organ>"; the "__"
        // plus the organ must remain part of the number (the Arango _key), not
        // split the term into more than two tokens (Springbok-LLC/nlm-ckn-etl#55
        // regression: organ-keyed CellSetDatasets were silently dropped).
        var node = NodeFactory.createURI(
                "http://purl.obolibrary.org/obo/CSD_2b1f9ac3-1234-5678-9abc-def012345678__respiratory_system");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertEquals("CSD", vtuple.id());
        assertEquals("2b1f9ac3-1234-5678-9abc-def012345678__respiratory_system", vtuple.number());
        assertTrue(vtuple.isValidVertex());
    }

    @Test
    void createVTuple_delimiterWithEmptyNumberIsInvalid() {
        // A trailing delimiter with no local identifier is not a vertex.
        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/CSD_");
        OntologyGraphBuilder.VTuple vtuple = OntologyGraphBuilder.createVTuple(node);

        assertFalse(vtuple.isValidVertex());
    }

    // --- taxon-constraint predicate tests (no ArangoDB needed) ---

    @Test
    void isTaxonConstraintPredicate_excludesTaxonConstraints() {
        assertTrue(OntologyGraphBuilder.isTaxonConstraintPredicate("RO_0002160"), "only_in_taxon");
        assertTrue(OntologyGraphBuilder.isTaxonConstraintPredicate("RO_0002161"), "never_in_taxon");
        assertTrue(OntologyGraphBuilder.isTaxonConstraintPredicate("RO_0002162"), "in_taxon");
    }

    @Test
    void isTaxonConstraintPredicate_allowsOtherRelations() {
        assertFalse(OntologyGraphBuilder.isTaxonConstraintPredicate("RO_0002175"), "present_in_taxon is kept");
        assertFalse(OntologyGraphBuilder.isTaxonConstraintPredicate("subClassOf"), "subClassOf is kept");
        assertFalse(OntologyGraphBuilder.isTaxonConstraintPredicate("RO_0002202"), "develops_from is kept");
    }

    // --- parsePredicate tests (no ArangoDB needed) ---

    @Test
    void parsePredicate_fragmentUri() {
        // A URI with a fragment should return the fragment
        var node = NodeFactory.createURI("http://www.w3.org/2000/01/rdf-schema#subClassOf");
        Map<String, OntologyElementMap> maps = new HashMap<>();
        maps.put("ro", new OntologyElementMap());

        String label = OntologyGraphBuilder.parsePredicate(maps, node).label();
        assertEquals("subClassOf", label);
    }

    @Test
    void parsePredicate_oboTermWithDevelopsFrom() {
        // A URI without fragment, where the term is in the ro map
        List<Path> roFile = List.of(OBO_DIR.resolve("ro.owl"));
        Map<String, OntologyElementMap> maps = OntologyElementParser.parseOntologyElements(roFile);

        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/RO_0002202");
        String label = OntologyGraphBuilder.parsePredicate(maps, node).label();
        assertEquals("develops from", label);
    }

    @Test
    void parsePredicate_oboTermWithCapableOf() {
        List<Path> roFile = List.of(OBO_DIR.resolve("ro.owl"));
        Map<String, OntologyElementMap> maps = OntologyElementParser.parseOntologyElements(roFile);

        var node = NodeFactory.createURI("http://purl.obolibrary.org/obo/RO_0002215");
        String label = OntologyGraphBuilder.parsePredicate(maps, node).label();
        assertEquals("capable of", label);
    }

    @Test
    void parsePredicate_nonUriThrows() {
        var node = NodeFactory.createLiteralString("not a URI");
        Map<String, OntologyElementMap> maps = new HashMap<>();
        maps.put("ro", new OntologyElementMap());

        assertThrows(RuntimeException.class, () -> OntologyGraphBuilder.parsePredicate(maps, node));
    }

    // --- normalizeEdgeSource tests ---

    @Test
    void normalizeEdgeSource_mondoSimple() {
        assertEquals("MONDO", OntologyGraphBuilder.normalizeEdgeSource("mondo-simple"));
    }

    @Test
    void normalizeEdgeSource_taxslim() {
        assertEquals("NCBITAXON", OntologyGraphBuilder.normalizeEdgeSource("taxslim"));
    }

    @Test
    void normalizeEdgeSource_goPlus() {
        assertEquals("GO", OntologyGraphBuilder.normalizeEdgeSource("go-plus"));
    }

    @Test
    void normalizeEdgeSource_uberonBase() {
        assertEquals("UBERON", OntologyGraphBuilder.normalizeEdgeSource("uberon-base"));
    }

    @Test
    void normalizeEdgeSource_defaultUpperCase() {
        assertEquals("CL", OntologyGraphBuilder.normalizeEdgeSource("cl"));
        assertEquals("HP", OntologyGraphBuilder.normalizeEdgeSource("hp"));
        assertEquals("PATO", OntologyGraphBuilder.normalizeEdgeSource("pato"));
    }

    // --- normalizeEdgeLabel tests ---

    @Test
    void normalizeEdgeLabel_subClassOf() {
        assertEquals("SUB_CLASS_OF", OntologyGraphBuilder.normalizeEdgeLabel("subClassOf"));
    }

    @Test
    void normalizeEdgeLabel_disjointWith() {
        assertEquals("DISJOINT_WITH", OntologyGraphBuilder.normalizeEdgeLabel("disjointWith"));
    }

    @Test
    void normalizeEdgeLabel_selectivelyExpresses() {
        // The UI filters marker gene edges on this label, and its counterpart, EXPRESSES.
        assertEquals("SELECTIVELY_EXPRESSES", OntologyGraphBuilder.normalizeEdgeLabel("selectively expresses"));
        assertEquals("EXPRESSES", OntologyGraphBuilder.normalizeEdgeLabel("expresses"));
    }

    @Test
    void normalizeEdgeLabel_isAbout() {
        assertEquals("IS_ABOUT", OntologyGraphBuilder.normalizeEdgeLabel("is about"));
    }

    @Test
    void normalizeEdgeLabel_crossSpeciesExactMatch() {
        assertEquals("CROSS_SPECIES_EXACT_MATCH", OntologyGraphBuilder.normalizeEdgeLabel("crossSpeciesExactMatch"));
    }

    @Test
    void normalizeEdgeLabel_exactMatch() {
        assertEquals("EXACT_MATCH", OntologyGraphBuilder.normalizeEdgeLabel("exactMatch"));
    }

    @Test
    void normalizeEdgeLabel_equivalentClass() {
        assertEquals("EQUIVALENT_CLASS", OntologyGraphBuilder.normalizeEdgeLabel("equivalentClass"));
    }

    @Test
    void normalizeEdgeLabel_seeAlso() {
        assertEquals("SEE_ALSO", OntologyGraphBuilder.normalizeEdgeLabel("seeAlso"));
    }

    @Test
    void normalizeEdgeLabel_describedIn() {
        assertEquals("DESCRIBED_IN", OntologyGraphBuilder.normalizeEdgeLabel("describedIn"));
    }

    @Test
    void normalizeEdgeLabel_defaultWithSpaces() {
        assertEquals("DEVELOPS_FROM", OntologyGraphBuilder.normalizeEdgeLabel("develops from"));
        assertEquals("CAPABLE_OF", OntologyGraphBuilder.normalizeEdgeLabel("capable of"));
        assertEquals("PART_OF", OntologyGraphBuilder.normalizeEdgeLabel("part of"));
    }

    @Test
    void normalizeEdgeLabel_defaultUpperCase() {
        assertEquals("LABEL", OntologyGraphBuilder.normalizeEdgeLabel("label"));
    }

    // --- getDocumentCollectionName tests ---

    @Test
    void getDocumentCollectionName_vertexId() {
        assertEquals("CL", OntologyGraphBuilder.getDocumentCollectionName("CL/0000235"));
    }

    @Test
    void getDocumentCollectionName_edgeId() {
        assertEquals("CL-GO", OntologyGraphBuilder.getDocumentCollectionName("CL-GO/0000235-0031268"));
    }

    @Test
    void getDocumentCollectionName_nullInput() {
        assertNull(OntologyGraphBuilder.getDocumentCollectionName(null));
    }

    @Test
    void getDocumentCollectionName_noSlash() {
        assertNull(OntologyGraphBuilder.getDocumentCollectionName("CL0000235"));
    }

    // --- getDocumentKey tests ---

    @Test
    void getDocumentKey_vertexId() {
        assertEquals("0000235", OntologyGraphBuilder.getDocumentKey("CL/0000235"));
    }

    @Test
    void getDocumentKey_edgeId() {
        assertEquals("0000235-0031268", OntologyGraphBuilder.getDocumentKey("CL-GO/0000235-0031268"));
    }

    @Test
    void getDocumentKey_nullInput() {
        assertNull(OntologyGraphBuilder.getDocumentKey(null));
    }

    @Test
    void getDocumentKey_noSlash() {
        assertNull(OntologyGraphBuilder.getDocumentKey("CL0000235"));
    }


    @Test
    void addSearchField_setsFieldOnDocumentWithoutIt() {
        BaseDocument doc = new BaseDocument("0002405");
        OntologyGraphBuilder.addSearchField(doc, "UBERON");
        assertEquals(List.of("UBERON:0002405",
                "UBERON_0002405",
                "http://purl.obolibrary.org/obo/UBERON_0002405",
                "https://purl.obolibrary.org/obo/UBERON_0002405"), doc.getAttribute("_search"));
    }

    @Test
    void addSearchField_replacesExistingValue() {
        BaseDocument doc = new BaseDocument("abc123");
        doc.addAttribute("_search", List.of("stale"));
        OntologyGraphBuilder.addSearchField(doc, "CS");
        assertEquals(List.of("CS:abc123", "CS_abc123"), doc.getAttribute("_search"));
    }
}
