package gov.nih.nlm;

import com.arangodb.ArangoDatabase;
import com.arangodb.ArangoEdgeCollection;
import com.arangodb.ArangoGraph;
import com.arangodb.ArangoVertexCollection;
import com.arangodb.entity.BaseDocument;
import com.arangodb.entity.BaseEdgeDocument;
import com.arangodb.model.AqlQueryOptions;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static gov.nih.nlm.AqlQuerySetBuilder.AqlQuerySet;
import static gov.nih.nlm.AqlQuerySetBuilder.getQuerySetInFour;
import static gov.nih.nlm.AqlQuerySetBuilder.getQuerySetInFourWithHierarchy;
import static gov.nih.nlm.AqlQuerySetBuilder.getQuerySetInThree;
import static gov.nih.nlm.AqlQuerySetBuilder.getQuerySetInThreeWithHierarchy;
import static gov.nih.nlm.AqlQuerySetBuilder.getQuerySetInTwo;
import static gov.nih.nlm.AqlQuerySetBuilder.getQuerySetInTwoWithHierarchy;
import static gov.nih.nlm.OntologyGraphBuilder.getDocumentCollectionName;

/**
 * Queries the fully populated ontology ArangoDB to identify all paths that connect cell set vertices inward to UBERON
 * then NCBITaxon vertices, and outward to gene then disease and drug vertices. The longest path outbound from each
 * UBERON, NCBITaxon, and MONDO nodes are included. Collects unique vertex and edge documents, then inserts them in the
 * phenotype ArangoDB.
 *
 * @deprecated Replaced by {@link InducedGraphBuilder}. This class will be removed in a future release.
 */
@Deprecated
public class PhenotypeGraphBuilder {

    // Construct ArangoDB utilities
    private static final ArangoDbUtilities arangoDbUtilities = new ArangoDbUtilities();

    /**
     * Query a fully populated ontology ArangoDB to identify all paths that connect cell set vertices inward to UBERON
     * then NCBITaxon vertices, and outward to gene then disease and drug vertices.
     *
     * @param databaseName Name of database containing fully populated graph
     * @param graphName    Name of fully populated graph
     * @return All identified paths
     */
    private static List<Map<String, Object>> getPaths(String databaseName, String graphName) {

        List<AqlQuerySet> aqlQuerySets = new ArrayList<>();

        // CS - CSD - PUB
        aqlQuerySets.add(getQuerySetInTwo(graphName, "CSD", "PUB"));
        // CS - CL - GS
        aqlQuerySets.add(getQuerySetInTwo(graphName, "CL", "GS"));
        // CS - CL - PR
        aqlQuerySets.add(getQuerySetInTwo(graphName, "CL", "PR"));
        // CS - BMC - GS
        aqlQuerySets.add(getQuerySetInTwo(graphName, "BMC", "GS"));
        // CS - BMC - BGS
        aqlQuerySets.add(getQuerySetInTwo(graphName, "BMC", "BGS"));

        // CS - UBERON - NCBITaxon +
        aqlQuerySets.add(getQuerySetInTwoWithHierarchy(graphName,
                "UBERON",
                "NCBITaxon",
                "NCBITaxon-NCBITaxon",
                "SUB_CLASS_OF"));
        // CS - CL - MONDO +
        aqlQuerySets.add(getQuerySetInTwoWithHierarchy(graphName, "CL", "MONDO", "MONDO-MONDO", "SUB_CLASS_OF"));
        // CS - CL - GO +
        aqlQuerySets.add(getQuerySetInTwoWithHierarchy(graphName, "CL", "GO", "GO-GO", "SUB_CLASS_OF"));
        // CS - CL - PATO +
        aqlQuerySets.add(getQuerySetInTwoWithHierarchy(graphName, "CL", "PATO", "PATO-PATO", "SUB_CLASS_OF"));

        // CS - CL - GS - RS
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "GS", "RS"));
        // CS - CL - GS - CHEMBL
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "GS", "CHEMBL"));
        // CS - CL - GS - PR
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "GS", "PR"));

        // CS - BMC - GS - RS
        aqlQuerySets.add(getQuerySetInThree(graphName, "BMC", "GS", "RS"));
        // CS - BMC - GS - CHEMBL
        aqlQuerySets.add(getQuerySetInThree(graphName, "BMC", "GS", "CHEMBL"));
        // CS - BMC - GS - PR
        aqlQuerySets.add(getQuerySetInThree(graphName, "BMC", "GS", "PR"));

        // CS - CL - PR - GS
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "PR", "GS"));
        // CS - CL - PR - CHEMBL
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "PR", "CHEMBL"));

        // CS - CL - MONDO - GS
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "MONDO", "GS"));
        // CS - CL - MONDO - RS
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "MONDO", "RS"));
        // CS - CL - MONDO - CHEMBL
        aqlQuerySets.add(getQuerySetInThree(graphName, "CL", "MONDO", "CHEMBL"));

        // CS - BMC - GS - MONDO +
        aqlQuerySets.add(getQuerySetInThreeWithHierarchy(graphName,
                "BMC",
                "GS",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));
        // CS - CL - PR - GO +
        aqlQuerySets.add(getQuerySetInThreeWithHierarchy(graphName, "CL", "PR", "GO", "GO-GO", "SUB_CLASS_OF"));
        // CS - CL - GO - NCBITaxon +
        aqlQuerySets.add(getQuerySetInThreeWithHierarchy(graphName,
                "CL",
                "GO",
                "NCBITaxon",
                "NCBITaxon-NCBITaxon",
                "SUB_CLASS_OF"));
        // CS - CL - GO - HsapDv +
        aqlQuerySets.add(getQuerySetInThreeWithHierarchy(graphName,
                "CL",
                "GO",
                "HsapDv",
                "HsapDv-HsapDv",
                "SUB_CLASS_OF"));
        // CS - CL - GO - UBERON +
        aqlQuerySets.add(getQuerySetInThreeWithHierarchy(graphName, "CL", "GO", "UBERON", "UBERON-UBERON", "PART_OF"));
        // CS - CL - PATO - UBERON +
        aqlQuerySets.add(getQuerySetInThreeWithHierarchy(graphName,
                "CL",
                "PATO",
                "UBERON",
                "UBERON-UBERON",
                "PART_OF"));

        // CS - CL - GS - MONDO - CHEMBL
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "GS", "MONDO", "CHEMBL"));
        // CS - CL - GS - CHEMBL - PR
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "GS", "CHEMBL", "PR"));
        // CS - CL - GS - PR - CHEMBL
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "GS", "PR", "CHEMBL"));

        // CS - BMC - GS - MONDO - CHEMBL
        aqlQuerySets.add(getQuerySetInFour(graphName, "BMC", "GS", "MONDO", "CHEMBL"));
        // CS - BMC - GS - CHEMBL - PR
        aqlQuerySets.add(getQuerySetInFour(graphName, "BMC", "GS", "CHEMBL", "PR"));
        // CS - BMC - GS - PR - CHEMBL
        aqlQuerySets.add(getQuerySetInFour(graphName, "BMC", "GS", "PR", "CHEMBL"));

        // CS - CL - PR - GS - RS
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "PR", "GS", "RS"));
        // CS - CL - PR - GS - CHEMBL
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "PR", "GS", "CHEMBL"));
        // CS - CL - PR - CHEMBL - GS
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "PR", "CHEMBL", "GS"));

        // CS - CL - MONDO - GS - RS
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "MONDO", "GS", "RS"));
        // CS - CL - MONDO - GS - CHEMBL
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "MONDO", "GS", "CHEMBL"));
        // CS - CL - MONDO - GS - PR
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "MONDO", "GS", "PR"));
        // CS - CL - MONDO - RS - GS
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "MONDO", "RS", "GS"));
        // CS - CL - MONDO - CHEMBL - GS
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "MONDO", "CHEMBL", "GS"));
        // CS - CL - MONDO - CHEMBL - PR
        aqlQuerySets.add(getQuerySetInFour(graphName, "CL", "MONDO", "CHEMBL", "PR"));

        // CS - CL - GS - RS - MONDO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName,
                "CL",
                "GS",
                "RS",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));
        // CS - CL - GS - CHEMBL - MONDO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName,
                "CL",
                "GS",
                "CHEMBL",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));
        // CS - CL - GS - PR - GO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName, "CL", "GS", "PR", "GO", "GO-GO", "SUB_CLASS_OF"));
        // CS - BMC - GS - RS - MONDO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName,
                "BMC",
                "GS",
                "RS",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));
        // CS - BMC - GS - CHEMBL - MONDO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName,
                "BMC",
                "GS",
                "CHEMBL",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));
        // CS - BMC - GS - PR - GO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName, "BMC", "GS", "PR", "GO", "GO-GO", "SUB_CLASS_OF"));
        // CS - CL - PR - GS - MONDO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName,
                "CL",
                "PR",
                "GS",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));
        // CS - CL - PR - CHEMBL - MONDO +
        aqlQuerySets.add(getQuerySetInFourWithHierarchy(graphName,
                "CL",
                "PR",
                "CHEMBL",
                "MONDO",
                "MONDO-MONDO",
                "SUB_CLASS_OF"));

        ArangoDatabase db = arangoDbUtilities.createOrGetDatabase(databaseName);
        AqlQueryOptions queryOpts = new AqlQueryOptions();
        List<Map<String, Object>> paths = new ArrayList<>();
        for (AqlQuerySet aqlQuerySet : aqlQuerySets) {
            System.out.println(aqlQuerySet.queryStr().lines().collect(Collectors.joining()).replaceAll("\\s+", " "));
            System.out.println(aqlQuerySet.bindVars());
            long startTime = System.nanoTime();
            @SuppressWarnings("unchecked") List<Map<String, Object>> queryPaths = (List<Map<String, Object>>) (List<?>) db.query(
                    aqlQuerySet.queryStr(),
                    Map.class,
                    aqlQuerySet.bindVars(),
                    queryOpts).asListRemaining();
            paths.addAll(queryPaths);
            long stopTime = System.nanoTime();
            System.out.println("Collected " + queryPaths.size() + " paths in " + (stopTime - startTime) / 1e9 + " s");
        }
        return paths;
    }

    /**
     * Collect unique vertex documents from all identified paths.
     *
     * @param paths All identified paths
     * @return Unique vertex documents
     */
    static List<BaseDocument> getVertexDocuments(List<Map<String, Object>> paths) {
        System.out.println("Collecting unique vertex documents from " + paths.size() + " identified paths");
        long startTime = System.nanoTime();
        List<BaseDocument> vertexDocuments = new ArrayList<>();
        Set<String> seenVertexIds = new HashSet<>();
        for (Map<String, Object> path : paths) {
            ArrayList<LinkedHashMap> vertices = (ArrayList<LinkedHashMap>) path.get("vertices");
            for (LinkedHashMap vertex : vertices) {
                BaseDocument vertexDoc = new BaseDocument(vertex);
                if (seenVertexIds.add(vertexDoc.getId())) {
                    vertexDocuments.add(vertexDoc);
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Collected " + vertexDocuments.size() + " unique vertex documents from " + paths.size() + " identified paths in " + (stopTime - startTime) / 1e9 + " s");
        return vertexDocuments;
    }

    /**
     * Collect unique edge documents from all identified paths.
     *
     * @param paths All identified paths
     * @return Unique edge documents
     */
    static List<BaseEdgeDocument> getEdgeDocuments(List<Map<String, Object>> paths) {
        System.out.println("Collecting unique edge documents from " + paths.size() + " identified paths");
        long startTime = System.nanoTime();
        List<BaseEdgeDocument> edgeDocuments = new ArrayList<>();
        Set<String> seenEdgeIds = new HashSet<>();
        for (Map<String, Object> path : paths) {
            ArrayList<LinkedHashMap> edges = (ArrayList<LinkedHashMap>) path.get("edges");
            for (LinkedHashMap edge : edges) {
                BaseEdgeDocument edgeDoc = new BaseEdgeDocument(edge);
                if (seenEdgeIds.add(edgeDoc.getId())) {
                    edgeDocuments.add(edgeDoc);
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Collected " + edgeDocuments.size() + " unique edge documents from " + paths.size() + " identified paths in " + (stopTime - startTime) / 1e9 + " s");
        return edgeDocuments;
    }

    /**
     * Insert unique vertex documents.
     *
     * @param phenotypeVertexDocuments Unique vertex documents
     * @param phenotypeGraph           Graph in phenotype database
     * @param ontologyGraph            Graph in ontology database
     */
    private static void insertVertexDocuments(List<BaseDocument> phenotypeVertexDocuments,
                                              ArangoGraph phenotypeGraph,
                                              ArangoGraph ontologyGraph) {
        System.out.println("Inserting " + phenotypeVertexDocuments.size() + " vertex documents");
        long startTime = System.nanoTime();
        Map<String, ArangoVertexCollection> phenotypeVertexCollections = new HashMap<>();
        for (BaseDocument phenotypeVertexDocument : phenotypeVertexDocuments) {
            String id = getDocumentCollectionName(phenotypeVertexDocument.getId());
            String key = phenotypeVertexDocument.getKey();
            if (!phenotypeVertexCollections.containsKey(id)) {
                phenotypeVertexCollections.put(id, arangoDbUtilities.createOrGetVertexCollection(phenotypeGraph, id));
            }
            BaseDocument ontologyVertexDocument = ontologyGraph.vertexCollection(id).getVertex(key, BaseDocument.class);
            if (phenotypeVertexCollections.get(id).getVertex(key, BaseDocument.class) == null) {
                if (ontologyVertexDocument != null) {
                    phenotypeVertexCollections.get(id).insertVertex(ontologyVertexDocument);
                } else {
                    phenotypeVertexCollections.get(id).insertVertex(phenotypeVertexDocument);
                }
            } else {
                if (ontologyVertexDocument != null) {
                    phenotypeVertexCollections.get(id).replaceVertex(key, ontologyVertexDocument);
                } else {
                    phenotypeVertexCollections.get(id).replaceVertex(key, phenotypeVertexDocument);
                }
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Inserted " + phenotypeVertexDocuments.size() + " vertex documents in " + (stopTime - startTime) / 1e9 + " s");
    }

    /**
     * Insert unique edge documents.
     *
     * @param phenotypeEdgeDocuments Unique edge documents
     * @param phenotypeGraph         Graph in phenotype database
     */
    private static void insertEdgeDocuments(List<BaseEdgeDocument> phenotypeEdgeDocuments, ArangoGraph phenotypeGraph) {
        System.out.println("Inserting " + phenotypeEdgeDocuments.size() + " edge documents");
        long startTime = System.nanoTime();
        Map<String, ArangoEdgeCollection> edgeCollections = new HashMap<>();
        for (BaseEdgeDocument edgeDocument : phenotypeEdgeDocuments) {
            String idPair = getDocumentCollectionName(edgeDocument.getId());
            String key = edgeDocument.getKey();
            String idFrom = getDocumentCollectionName(edgeDocument.getFrom());
            String idTo = getDocumentCollectionName(edgeDocument.getTo());
            if (!edgeCollections.containsKey(idPair)) {
                edgeCollections.put(idPair, arangoDbUtilities.createOrGetEdgeCollection(phenotypeGraph, idFrom, idTo));
            }
            if (edgeCollections.get(idPair).getEdge(key, BaseEdgeDocument.class) == null) {
                edgeCollections.get(idPair).insertEdge(edgeDocument);
            } else {
                edgeCollections.get(idPair).replaceEdge(key, edgeDocument);
            }
        }
        long stopTime = System.nanoTime();
        System.out.println("Inserted " + phenotypeEdgeDocuments.size() + " edge documents in " + (stopTime - startTime) / 1e9 + " s");
    }

    /**
     * Query the fully populated ontology ArangoDB to identify all paths that connect cell set vertices inward to UBERON
     * then NCBITaxon vertices, and outward to gene then disease and drug vertices. Collect unique vertex and edge
     * documents, then insert them in the phenotype ArangoDB.
     */
    public static void main(String[] args) {

        // Get all phenotype database subgraph paths in the ontology database and fully
        // populated graph
        String ontologyDatabaseName = "Cell-KN-Ontologies";
        String ontologyGraphName = "KN-Ontologies-v2.0";
        ArangoDatabase ontologyDb = arangoDbUtilities.createOrGetDatabase(ontologyDatabaseName);
        ArangoGraph ontologyGraph = arangoDbUtilities.createOrGetGraph(ontologyDb, ontologyGraphName);
        List<Map<String, Object>> paths = getPaths(ontologyDatabaseName, ontologyGraphName);

        // Initialize the phenotype database and subgraph
        String phenotypeDatabaseName = "Cell-KN-Phenotypes";
        String phenotypeGraphName = "KN-Phenotypes-v2.0";
        arangoDbUtilities.deleteDatabase(phenotypeDatabaseName);
        ArangoDatabase phenotypeDb = arangoDbUtilities.createOrGetDatabase(phenotypeDatabaseName);
        arangoDbUtilities.deleteGraph(phenotypeDb, phenotypeGraphName);
        ArangoGraph phenotypeGraph = arangoDbUtilities.createOrGetGraph(phenotypeDb, phenotypeGraphName);

        // Get vertex documents in the ontology graph and insert them in the phenotype graph
        List<BaseDocument> phenotypeVertexDocuments = getVertexDocuments(paths);
        insertVertexDocuments(phenotypeVertexDocuments, phenotypeGraph, ontologyGraph);

        // Get edge documents in the ontology graph and insert them in the phenotype graph
        List<BaseEdgeDocument> phenotypeEdgeDocuments = getEdgeDocuments(paths);
        insertEdgeDocuments(phenotypeEdgeDocuments, phenotypeGraph);

        // Disconnect from a local ArangoDB server instance
        arangoDbUtilities.arangoDB.shutdown();
    }
}
