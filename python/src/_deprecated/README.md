# Deprecated Tuple Writers

These modules were the original hand-crafted tuple writers, replaced in
April 2026 by schema-driven tuple writers that use `ckn-schema` Pydantic
entity classes and a shared infrastructure module (`TupleWriterUtilities.py`).

## Replacements

| Deprecated module | Replaced by |
|---|---|
| `NSForestResultsTupleWriter.py` | `NSForestTupleWriter.py` |
| `AuthorToClResultsTupleWriter.py` | `MappingTupleWriter.py` |
| `ExternalApiResultsTupleWriter.py` | `CellxGeneTupleWriter.py`, `OpenTargetsTupleWriter.py`, `GeneTupleWriter.py`, `UniProtTupleWriter.py`, `HuBMAPTupleWriter.py` |
| `AnnotationResultsTupleWriter.py` | (functionality no longer used) |
| `SchemaBasedTupleWriter.py` | (exploratory prototype, superseded by all of the above) |
| `SchemaTupleWriter.py` | (intermediate version, split into the above modules) |

## Why kept

These are retained for reference during the transition to ensure tuple
consistency between old and new writers. They can be removed once the
new writers are validated in production.

## Comparison: Old vs New (2026-04-03)

Comparison run using `compare_tuples.py` on the li-2023 MVP dataset
and all external API data sources.

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
