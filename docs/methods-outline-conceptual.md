# Methods — Outline (scientific / conceptual)

A conceptual framing of NLM Cell Knowledge Network (NLM-CKN) construction.
Engineering realization (workflow orchestration, database snapshots, object
storage) is treated as implementation and confined to §7 and the supplement.
The process-oriented companion outline is `methods-outline.md`.

Sources consulted: `python/src/flows/{release,fetch,pipeline}.py`,
`python/src/*TupleWriter.py`, `src/main/java/gov/nih/nlm/{OntologySlimmer,
OntologyGraphBuilder, ResultsGraphBuilder, InducedSubgraphBuilder,
InducedSubgraphFinder}.java`, `data/nlm-ckn-collection-maps.json`.

---

## 1. Rationale and scope

- 1.1 **The problem.** Transcriptomic cell-type definitions — marker-gene
  combinations derived from single-cell atlases — and the biomedical knowledge
  that gives them meaning (anatomy, disease, protein function, drug and trial
  evidence) live in separate resources with incompatible identifier systems. A
  cell set discovered in one atlas cannot be routinely connected to the
  diseases, proteins, or therapies implicated by its markers.
- 1.2 **The claim.** These layers can be joined into a single, ontology-grounded
  graph in which an empirically derived cell set is a first-class entity, linked
  through explicit, provenanced relations to the ontology terms and external
  evidence that describe it — so that questions spanning the layers become
  traversals rather than manual cross-referencing.
- 1.3 **Scope of this build.** Human only (NCBITaxon:9606); cell sets derived
  from published single-cell atlases via NSForest marker selection; contextual
  evidence restricted to five public resources (§3.2). State the organs,
  atlases, and release version covered by the reported build.
- 1.4 **Unit of release.** Each build is a self-describing scientific artifact
  keyed to an immutable set of inputs, so any stated result is attributable to a
  specific combination of observations, ontology versions, and code (§6).

## 2. Knowledge representation

- 2.1 **Model.** A directed, typed, attributed multigraph. Assertions are
  produced as subject–predicate–object tuples against a shared schema
  (`ckn_schema`, LinkML-derived Pydantic entities) and only then materialized as
  vertices and edges. Committing to a schema — rather than writing directly to
  the graph store — is what makes each source's contribution auditable and makes
  the same assertion arriving from two sources reconcilable.
- 2.2 **Entity classes** and their scientific roles:

  | Class | Role |
  |---|---|
  | Cell set (CS) | An empirically derived cluster of cells from one dataset — the anchor entity |
  | Cell set dataset (CSD) | The dataset and version a cell set was derived from |
  | Binary gene set (BGS), Biomarker combination (BMC) | The marker-gene combination discriminating a cell set |
  | Gene symbol (GS), Protein (PR) | Molecular entities and their products |
  | Cell type (CL) | Ontology cell-type term a cell set is annotated to |
  | Anatomical structure (UBERON) | Tissue and organ context, with part-of structure |
  | Disease (MONDO), Phenotype (HP, PATO) | Clinical and phenotypic context |
  | Gene function (GO), Life-cycle stage (HsapDv), Taxon (NCBITaxon) | Ontological qualifiers |
  | Compound (CHEMBL), Clinical trial (NCT) | Therapeutic evidence |
  | Publication (PUB) | Bibliographic provenance of a dataset |

- 2.3 **Relations.** Predicates come from the schema's association classes —
  e.g. cell set → biomarker combination, gene → *produces* → protein,
  anatomical structure → *part of* → anatomical structure, cell set dataset →
  *was attributed to* → publication. Give the full predicate inventory as a
  supplementary table.
- 2.4 **Evidence travels on the edge.** Statistical support is an edge property,
  not a separate table: cell-set → biomarker-combination edges carry NSForest
  precision, TP/FP/TN/FN, marker count, and silhouette scores; anatomical edges
  record the tissue actually sampled. This is what allows a traversal to be
  filtered by evidence quality rather than taken on trust.
- 2.5 **Identity and grounding.** Every entity resolves to a persistent
  identifier — OBO PURLs for ontology terms, Ensembl / Entrez / UniProt / ChEMBL
  for molecular and therapeutic entities. Describe cross-identifier resolution
  and the minimum-cluster-size and deprecated-term policies.

## 3. Evidence sources

- 3.1 **Primary observations.** Per-organ, per-dataset NSForest results
  (marker-gene combinations with discriminative statistics), author-assigned
  cell-type labels, and the manual author-label → Cell Ontology mapping that
  grounds them. Report organ and dataset counts and the provenance of the manual
  mapping.
- 3.2 **Contextual resources**, each contributing a defined slice of the graph:
  - **CELLxGENE** — dataset metadata; attribution of cell set datasets to
    publications.
  - **NCBI Gene** — gene identity, nomenclature, gene → protein product.
  - **UniProt** — protein-level annotation.
  - **Open Targets** — gene/protein associations to disease, mutation, drug, and
    clinical trial.
  - **HuBMAP ASCT+B** — anatomical part-of structure for organ context.
- 3.3 **Ontologies as connective tissue:** CL, UBERON, PR, MONDO, HP, PATO, GO,
  HsapDv, NCBITaxon, Orphanet, CHEBI. Report source IRIs, release versions, and
  access dates in a supplementary table.
- 3.4 **Query scope is data-driven.** External resources are queried only for the
  genes the primary observations actually implicate: the network is grown
  outward from the data rather than assembled from whole reference databases.
  This bounds its size and makes coverage an interpretable quantity (§5.2).

## 4. Construction

### 4.1 Ontological substrate

- Ontologies are loaded first, as the layer into which all evidence is
  subsequently placed, so every downstream assertion attaches to a term that
  already carries a hierarchy.
- **Species restriction.** Because the build is human-scoped, two ontologies are
  reduced before loading, by opposite logic appropriate to each:
  - *Protein Ontology* — **inclusion**: retain only protein classes explicitly
    asserted to be in a permitted taxon. PR is dominated by non-human orthologs,
    which would inflate the graph without supporting human-relevant inference.
  - *UBERON* — **exclusion**: retain all anatomy except classes whose taxon
    constraints (`never_in_taxon`, `only_in_taxon`, `in_taxon`), resolved against
    the NCBITaxon hierarchy, rule out every permitted taxon. Anatomy is largely
    taxon-neutral, so exclusion preserves more usable structure than inclusion
    would.
  - Residual taxon references on retained classes are resolved at load: only
    permitted-taxon vertices are created, and taxon-constraint relations are not
    carried into the graph.
- Report the reduction achieved (classes before/after) for each ontology.

### 4.2 Evidence integration

- Each source is translated by a dedicated writer into schema-conformant tuples,
  in a fixed order chosen so entities exist before they are annotated: NSForest →
  author-to-CL mapping → CELLxGENE → Open Targets → NCBI Gene → UniProt →
  HuBMAP.
- Loading those tuples onto the ontological substrate yields the **integrated
  graph**: cell sets and their marker combinations attached to ontology terms,
  and those terms in turn attached to molecular, clinical, and bibliographic
  evidence.
- State merge semantics: when two sources assert the same entity or edge, how
  identity is decided and how conflicting attributes are resolved.

### 4.3 The phenotype subgraph

- The integrated graph retains ontology material unreachable from any
  observation. The released **phenotype graph** is therefore an *induced
  subgraph*, defined by an explicit and reproducible rule. This is a scientific
  judgment about what constitutes the evidence-supported neighborhood of the
  cell sets, and should be argued as one.
- **Rule.** Multi-source breadth-first traversal seeded at *all* cell-set
  vertices, to a bounded depth (10), retaining every vertex reached together with
  all edges among the retained set.
- **Exclusions**, each with a stated reason:
  - Compound (CHEBI) and clinical-trial (NCT) vertices are not traversed
    *through* — they are evidence endpoints, not connectors.
  - The protein→taxon relation is not traversed at all: since every protein is
    annotated with its source organism, this one edge class would pull the entire
    protein space into the neighborhood through a single uninformative hub. The
    general principle: high-fan-out, low-information relations are cut so that
    reachability remains a meaningful proxy for relatedness.
- **Hierarchy completion.** Reachability alone truncates ontology hierarchies
  mid-path, leaving terms without ancestors and breaking generalization queries.
  Ancestor paths are restored per ontology under an explicit policy: the full
  hierarchy for CL (the cell-type backbone); ancestor walks to root along
  `SUB_CLASS_OF` for GO, MONDO, HP, PATO, HsapDv, NCBITaxon, Orphanet, and
  CHEBI; ancestor walks along `PART_OF` for UBERON, the meaningful anatomical
  relation; and none for PR.
- Report the induction's effect: vertices and edges before and after, by class.

### 4.4 Access layer

- Text analyzers and a unified search view span both graphs, so entities are
  retrievable by label and synonym as well as by identifier; per-class display
  and linking conventions are declared in a collection map.
- Each vertex carries a `_search` field of identifier forms: CURIE, underscore
  form, and PURL for OBO ontology terms, and the colon and underscore forms of
  collection and key for all other vertices. A case-folding analyzer lets a
  pasted or typed identifier match regardless of case.

## 5. Characterization of the released network

- 5.1 **Composition** — vertex and edge counts by class and by contributing
  source; organ and dataset coverage.
- 5.2 **Grounding** — fraction of author cell-type labels mapped to CL; fraction
  of marker genes resolved in each external resource. The honest statement of how
  complete the joins actually are.
- 5.3 **Evidence distribution** — NSForest precision / F-beta and silhouette-score
  distributions over retained cell-set–biomarker edges.
- 5.4 **Topology** — connectivity of the phenotype subgraph, and how much of it
  derives from observation versus hierarchy completion.
- 5.5 **Illustrative traversals** demonstrating the claim in §1.2 — e.g. cell set
  → markers → protein → disease → trial — presented as worked examples rather
  than benchmarks.

## 6. Reproducibility and provenance

- 6.1 Each build is identified by its inputs, its run, and the content hash of
  the code that produced it; derived intermediates are stored under that hash, so
  an intermediate is never reused with code that did not generate it.
- 6.2 Recorded with every release: build and evidence-retrieval timestamps,
  source repository commits, per-source retrieval outcomes and payload sizes,
  ontology versions, and dependency versions.
- 6.3 Evidence snapshots are versioned with the release, so a build remains
  reconstructible as the live external APIs change beneath it.
- 6.4 Freshness policy stated in scientific terms: what counts as a stale
  evidence snapshot, and what triggers a rebuild.

## 7. Implementation and availability

- 7.1 Workflow orchestration, graph store, and compute environment (brief; detail
  in the supplement).
- 7.2 Validation gates ensuring an incomplete build fails rather than publishes:
  input completeness, source-file integrity against a manifest, non-empty
  assertion output, and conformance of the released data to the production data
  specification.
- 7.3 Code, released graph dump, and build manifest — repositories, tags, DOIs.

---

### To supply from outside the codebase

- Ontology IRIs, versions, and access dates; external API versions and access
  dates.
- Provenance and curation protocol for the manual author-label → CL mapping.
- Justification for the BFS depth bound (10) and the minimum cluster size.
- All counts, coverage fractions, and distributions in §4–5.
