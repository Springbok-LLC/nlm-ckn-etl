# Methods — Outline

Derived from the orchestration code: `python/src/flows/release.py` (top-level
release flow), `python/src/flows/fetch.py` (external API acquisition), and
`python/src/flows/pipeline.py` (three-phase ETL).

---

## 1. Overview

- 1.1 Goal: assemble a versioned, reproducible cell-phenotype knowledge network
  (NLM-CKN) as a property graph in ArangoDB, from cell-type marker-gene results
  plus five external biomedical data sources and a set of OWL ontologies.
- 1.2 Design principles to state up front:
  - **Release-driven**: every build is keyed to an immutable upstream release
    tag (`nlm_ckn_tag`) on the `NIH-NLM/nlm-ckn` repository; the tag names the
    input data artifact, the run, and all derived outputs.
  - **Phase-isolated and restartable**: each of the three ETL phases restores
    its predecessor's database snapshot and writes its own, so any phase can be
    re-run independently.
  - **Content-addressed**: the compiled Java JAR is hashed (`jar_key`) and the
    ontology baseline snapshot is stored under that hash, guaranteeing that a
    snapshot is only ever reused with the code that produced it.
  - **Provenance-recording**: every promoted artifact carries a `build-info.txt`
    with build timestamp, git commit, run name, JAR key, external-fetch
    timestamp and per-source file sizes, and language/tool versions.
- 1.3 Implementation: Prefect flows (Python) orchestrating Python transformers
  and Java graph builders; ArangoDB in Docker; artifacts in versioned S3
  prefixes. Figure 1 = pipeline schematic (release → fetch → 3-phase ETL).

## 2. Input data

- 2.1 **Upstream release artifact** — `prod-data-<tag>.tar.gz`, a GitHub Release
  asset containing per-organ, per-dataset NSForest marker-gene results under
  `data/prod/<organ>/…`. On extraction the tree is flattened into a single
  run-scoped results directory; per-dataset filenames encode organ, author, and
  year and so remain unique.
  - Per-organ `master_s3_manifest.csv` files are unioned (rather than
    flattened onto one name) so the downstream file-integrity cross-check
    covers every organ rather than the last one extracted.
  - Acceptance check: the archive must contain at least one
    `results_ensg_*.csv` (the canonical Ensembl-ID NSForest naming); an empty
    match aborts the run before any expensive step.
- 2.2 **Ontologies** — OWL sources downloaded per run (Cell Ontology, UBERON,
  PR, NCBITaxon/taxslim, etc.; enumerate exact IRIs and access dates in a
  supplementary table).
- 2.3 **External resources** — CELLxGENE, Open Targets, NCBI Gene (E-Utilities),
  UniProt, and HuBMAP ASCT+B. HuBMAP endpoint URLs are declared in the
  release configuration (`release.json`) and shipped with the run inputs.
- 2.4 **Run configuration** — `release.json` pins tag, source repository,
  tarball location, and the maximum acceptable external-cache age; placeholder
  values are rejected at flow start.

## 3. External data acquisition (fetch stage)

- 3.1 Gene scope is defined by the release: the NSForest marker genes determine
  which identifiers are queried against each external API — so the fetch is
  validated against the presence of the release results directory before any
  request is issued.
- 3.2 Caching and freshness policy:
  - A cache is reused only if it exists, is younger than a configurable age
    (default 672 h = 4 weeks), and was produced by the *same fetch code*
    (content hash of the fetch modules). Otherwise a complete re-fetch is
    forced. Release runs force a fresh, date-stamped snapshot.
  - Failed API calls are recorded as empty entries; a retry pass strips only
    those entries so transient failures are recovered without discarding
    successful results.
  - Per-source status (`last_outcome`, `last_success_at`) is tracked so
    already-fresh sources can be skipped.
- 3.3 Normalization: raw responses are transformed into per-source
  `*_transformed.json` records consumed by the tuple writers; transformation is
  idempotent (skipped when output is newer than input).
- 3.4 Validation and provenance: required raw and transformed files are checked
  for presence and non-emptiness; `fetch-info.json` records timestamp, git
  commit, fetch-code hash, validation status, and per-file sizes. Data that
  fails validation is retained locally for resumption but never promoted to
  shared storage.

## 4. Knowledge-graph construction

### 4.1 Phase 1 — Ontology baseline

- Fresh database instance (data directory wiped) to guarantee a clean slate.
- Download OWL sources.
- **Taxon slimming**: `pr.owl → pr-slim.owl` retains only protein classes
  asserted in a permitted taxon (human, NCBITaxon:9606) — inclusion;
  `uberon-base.owl → uberon-human.owl` removes only classes whose
  `never_in_taxon` / `only_in_taxon` / `in_taxon` constraints exclude every
  permitted taxon, resolved against the NCBITaxon hierarchy — exclusion.
  Rationale: unfiltered PR is prohibitively large; unfiltered UBERON carries
  non-human anatomy. Surviving taxon references are handled at load time (only
  permitted-taxon vertices are created; taxon-constraint relations are dropped).
- Load the slimmed ontologies as vertices and edges into the
  `Cell-KN-Ontologies` database (named graph `KN-Ontologies-v2.0`).
- Snapshot the database (`arangodump`) as the **baseline**, keyed by JAR content
  hash; named-graph and analyzer definitions are exported as sidecar files
  because they live in system collections that the dump excludes.

### 4.2 Phase 2 — Evidence integration

- Restore the baseline snapshot (making the phase idempotent and repeatable
  without repeating the expensive ontology build) and recreate the ontology
  named graph from its sidecar.
- Validate inputs: release results directory and the external cache.
- **Tuple writing** — subject–predicate–object records are emitted, in order,
  by source-specific writers: NSForest (cell type ↔ marker gene), ontology term
  mapping, CELLxGENE, Open Targets, NCBI Gene, UniProt, and HuBMAP ASCT+B.
  Describe here, per writer: source records → vertex/edge types, identifier
  normalization (Ensembl ↔ Entrez ↔ UniProt ↔ symbol), and the slot contract
  each writer must satisfy. Empty tuple output aborts the run.
- Load tuples into the ontology graph (results graph builder), producing the
  integrated `Cell-KN-Ontologies` graph.
- Create text analyzers and a unified ArangoSearch view over the collection map.
- Snapshot as the **results dump**, keyed by JAR hash *and* run name.

### 4.3 Phase 3 — Phenotype subgraph and release

- Restore the results snapshot when the phase runs standalone.
- **Induced subgraph**: derive `Cell-KN-Phenotypes` (`KN-Phenotypes-v2.0`) from
  the integrated ontology + evidence graph. Define here the induction rule —
  which vertex classes seed the subgraph and which edges are retained.
- Create analyzers and views for the phenotype database.
- Snapshot the fully built state as the **golden dump**.
- Promote to versioned storage: golden dump, OWL sources, the external-cache
  snapshot for the run, and `build-info.txt`, under a run-scoped prefix; the
  run's results directory is copied to a stable `latest` pointer so subsequent
  scheduled fetches target the current gene set.

## 5. Reproducibility, provenance, and versioning

- 5.1 Every artifact is addressable by (release tag, run name, JAR content hash).
- 5.2 The three snapshots (baseline / results / golden) are the recovery points;
  state which is published and which are internal.
- 5.3 Recorded metadata: build and fetch timestamps, git commits, JAR key,
  per-file cache sizes, Java/Python package versions, CI workflow and run URL.
- 5.4 Deployment: the released dump is restored downstream and its named graphs
  and analyzers recreated from the exported sidecars.

## 6. Validation and quality control

- 6.1 Fail-fast gates, in execution order: tarball reachability and NSForest
  naming; release-directory completeness (`results_ensg_*.csv`,
  `hubmap_urls.txt`); external-file presence and size; non-empty tuple output;
  presence of the required snapshot for each phase.
- 6.2 File-manifest cross-check against the unioned per-organ S3 manifest.
- 6.3 Production data specification / validator (`ProductionDataSpecification`,
  `ProductionDataValidator`) — describe the asserted slot contracts and the
  conformance report.
- 6.4 Graph-level summary statistics to report: vertex and edge counts by type
  and by source, per-organ dataset counts, identifier-mapping coverage rates
  (fraction of marker genes resolved in each external resource).

## 7. Availability

- 7.1 Code: ETL repository (`nlm-ckn-etl`) and release repository (`nlm-ckn`),
  with the tag used for the reported build.
- 7.2 Data: released database dump and accompanying `build-info.txt`.
- 7.3 Compute environment: containerized ArangoDB, JVM heap settings, and the
  managed batch/scheduling environment used for production runs.

---

### Notes for drafting

- Items to fill in from sources not covered by the orchestration code: exact
  ontology IRIs and versions, the tuple-writer slot contracts, the induced
  subgraph rule, and API access dates/versions for the five external resources.
- The extensive skip/force and cache-age logic is engineering detail; compress
  it in the paper to "snapshots are reused only when the producing code and
  inputs are unchanged," and put the full policy in supplementary methods.
