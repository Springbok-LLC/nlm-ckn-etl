# Deprecated Modules

This directory holds retired code: modules whose job the pipeline no longer
does at all. Nothing here is imported by the live pipeline, and `_deprecated`
is excluded from `ruff` and from the default `pytest` run (`norecursedirs`).

Keep what lands here **self-contained**. The modules that used to sit
alongside these imported live helpers from `src/`, and when those helpers
moved on — `LoaderUtilities.EXTERNAL_DIRPATH` became the run-scoped
`get_current_run().external_dir`, and `get_results_sources` was deleted
outright — the archive stopped importing without anyone noticing. An archive
that depends on live code is an archive that rots.

## The April 2026 transition (code removed)

The original hand-crafted tuple writers were replaced in April 2026 by the
schema-driven writers that use `ckn-schema` Pydantic entity classes and a
shared infrastructure module (`TupleWriterUtilities.py`):

| Superseded module | Replaced by |
|---|---|
| `NSForestResultsTupleWriter.py` | `NSForestTupleWriter.py` |
| `AuthorToClResultsTupleWriter.py` | `MappingTupleWriter.py` |
| `ExternalApiResultsTupleWriter.py` | `CellxGeneTupleWriter.py`, `OpenTargetsTupleWriter.py`, `GeneTupleWriter.py`, `UniProtTupleWriter.py`, `HuBMAPTupleWriter.py` |
| `AnnotationResultsTupleWriter.py` | (functionality no longer used) |
| `SchemaBasedTupleWriter.py` | (exploratory prototype, superseded by all of the above) |
| `SchemaTupleWriter.py` | (intermediate version, split into the above modules) |

Those modules, their helpers (`CellKnSchemaUtilities.py`,
`DatasetSummary.py`, `ExternalApiResultsFetcher.py`,
`OntologyParserLoader.py`), their tests, and the comparison scripts
(`compare_tuples.py`, `compare_fetcher_outputs.py`) were **deleted in
September 2026**, on the exit criterion this file already stated: they were
kept only until the replacements were validated in production, and the
replacements have been through releases rc.7 to rc.10.

They are in git history — the last commit to contain them is the parent of
the one that removed them. What was worth keeping was never the code but the
evidence that the replacements agree with them, and that is below and in
[`compare_tuples-2026-04-07.md`](../../tests/_deprecated/compare_tuples-2026-04-07.md),
which reports a fuller run of the same comparison.

## Retired capabilities

| Module | Retired |
|---|---|
| `HuBMAPTupleWriter.py` | September 2026 |
| `UberonHuBMAPAuditor.py` | September 2026 (never ran in production) |
| `InducedGraphBuilder.py` | September 2026 (never ran in production) |

`HuBMAPTupleWriter` asserted `AnatomicalStructure part_of AnatomicalStructure`
edges over UBERON terms, read from the HuBMAP CCF ASCT+B tables. It was the
only consumer of the fetched HuBMAP data.

It was retired because the hierarchy it asserted is the one UBERON already
supplies, and the two disagree. HuBMAP tuples load *after* the ontology
triples, and the loader keeps the last non-`None` value, so HuBMAP silently
won every disagreement: first over labels — addressed in 9b68a91 by dropping
`ccf_pref_label` — and then over the `part_of` edges themselves, which is what
this retirement addresses.

`UberonHuBMAPAuditor` is the tool built to adjudicate that: it compares the
UBERON tuples written by `gov.nih.nlm.OntologyTupleWriter` against the HuBMAP
tuples and writes a review workbook (label conflicts, HuBMAP-only `part_of`
edges, unknown or deprecated terms) with a `Decision` column per row. It reads
`HuBMAPTupleWriter`'s output, so the two are retired together — without the
writer there is nothing for it to audit.

`OntologyTupleWriter` itself is **not** retired: it dumps the triples the
ontology graph builder loads from an OWL file, which is useful independently
of this audit.

**To revive either:** move the module back to `src/`, and re-add
`HuBMAPTupleWriter` to `TupleWriterPipeline.py`. The HuBMAP fetch was left in
place (`DataFetcher.HuBMAPFetcher`, `release.json` → `hubmap_urls.txt`), so the
input data is still downloaded. The auditor additionally needs `openpyxl`,
which is not a declared dependency.

`InducedGraphBuilder.py` is a Python implementation of the same BFS-plus-
hierarchy-walk induced subgraph build that `gov.nih.nlm.InducedSubgraphBuilder`
and `InducedSubgraphFinder` (Java) perform: multi-source BFS from `CS` out to
`MAX_DEPTH`, skipping `IGNORED_COLLECTIONS`, then backfilling ontology
hierarchy ancestors per `HIERARCHY_CONFIG`. It was never wired into
`flows/pipeline.py` — the Java version is, and is what every release has
actually run. Its own config had drifted out of sync with the Java version's
(for example it still listed GO as a "walk" hierarchy strategy after GO was
excluded from the induced graph entirely in the Java version;
Springbok-LLC/nlm-ckn-etl#119) with nothing to catch the drift, since nothing
ran it.

**To revive it:** move the module back to `src/`, reconcile its
`IGNORED_COLLECTIONS`/`HIERARCHY_CONFIG` against the current
`InducedSubgraphFinder.java`, and wire a call into `flows/pipeline.py`'s
Phase 3 in place of (or alongside, for comparison) the Java
`InducedSubgraphBuilder` invocation. It depends on `networkx`, `nx_arangodb`,
and `python-arango`, and reads via `nx_arangodb`/`ArangoClient` rather than
the JGraphT-based loader the Java version uses.

**To run the archived tests**, point `pytest` at them explicitly — the default
run skips this directory:

```
poetry run pytest tests/_deprecated/HuBMAPTupleWriterTestCase.py \
                  tests/_deprecated/UberonHuBMAPAuditorTestCase.py \
                  tests/_deprecated/InducedGraphBuilderTestCase.py
```

## Comparison: Old vs New (2026-04-03)

Comparison run on the li-2023 MVP dataset and all external API data sources,
using the `compare_tuples.py` removed with the code it compared.

### NSForest (3,617 tuples both old and new)

- **Relationships:** Identical -- same 5 predicates, same 735 triples
- **Edge annotations:** Identical -- same 904 CS-BMC quintuples
- **Only in old:** `#Binary_genes` on BGS nodes (113), `rdf#type` triples
  (113), 226 extra source quintuples on `dc#Source` relationships
- **Only in new:** `#Markers` on BGS (follows schema field name), richer
  CellSet annotations (`Author_cell_term`, `Biomarker_combination`,
  `Binary_gene_set`, `Expressed_genes`, `Species`)

### Mapping (old: 4,078 / new: 3,386)

- **Predicates:** Both now use `RO_0002294` (selectively_expresses)
- **Only in old:** 531 `Gene part_of CellType` (BFO_0000050) reverse
  triples (not a schema association), `#Cell_type` annotation on CellSet
- **Only in new:** Richer annotations -- 16 attribute types including
  `Label`, `Species`, `Publication`, `Anatomical_structure`, etc. on
  CellType, CellSet, CellSetDataset, Publication entities

### CELLxGENE (old: 1,350 / new: 1,035)

- **Relationships:** Identical (75 CSD-PUB + 75 source)
- **Only in old:** `Dataset_ID`, `Dataset_version_ID`,
  `Collection_version_ID` annotations (not schema fields);
  `Number_of_cells`/`Organism`/`Tissue` (renamed in schema)
- **Only in new:** Schema field names (`Species`,
  `Anatomical_structure`, `Total_cell_count`, `Cellxgene_collection`,
  `Cellxgene_dataset`)

### Open Targets (old: 566K / new: 722K)

- **Predicates:** Both use `RO_0020325` for `evaluated_in`
- **New adds 3 association types:** `DrugMolecularlyInteractsWithGene`,
  `GeneMolecularlyInteractsWithDrug`,
  `GeneGeneticallyInteractsWithGene` -- 117K new relationship triples
- **Vertex annotations deduplicated:** Old had 127K annotation tuples,
  new has 44K (each entity annotated once)
- **Only in old:** `#Indications` (list of MONDO terms), `#Target`,
  `#Synonyms` (ad-hoc attributes not in schema)
- **Only in new:** Schema field names (`Exact_synonym`, `Protein`,
  `Uniprot_id`, `Approval_status`, `Label`, `Definition`)

### Gene (old: 59,584 / new: 59,916)

- **Relationships:** Identical -- 4,615 Gene-Protein (produces)
- **Attribute renaming:** `Official_full_name` to `Label`, `Organism`
  to `Species`, `Summary` to `Refseq_summary`, `RefSeq_gene_ID` to
  `Reference_sequence_identifier`, `Official_symbol` to `Gene_symbol`
- **Data formatting:** `Also_known_as` is comma-joined string vs
  stringified Python list
- **New adds:** 331 extra tuples from `Gene_symbol` and `Also_known_as`
  annotations (previously missing for some genes)

### UniProt (old: 32,256 / new: 27,278)

- **Attribute renaming:** `Protein_name` to `Label`, `Gene_name` to
  `Gene_symbol`, `Organism` to `Species`
- **Old emitted:** `UniProt_ID` (term-encoded, correctly skipped in
  new), `Function: None` as string (new correctly skips None values,
  370 fewer tuples)
- **Formatting:** `Annotation_score` as `2` vs `2.0`

### HuBMAP (old: 366 / new: 549)

- **Relationships:** Identical -- 0 only in old
- **Only in new:** 179 `#Label` vertex annotations from `ccf_pref_label`
