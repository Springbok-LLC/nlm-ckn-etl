# Tuple Comparison Summary: OLD vs NEW Writers

Comparison run on 2026-04-07 using `compare_tuples.py` against the
li-2023 MVP dataset.

## Overview

| Writer | OLD tuples | NEW tuples | Common | Only OLD | Only NEW |
|---|---|---|---|---|---|
| NSForest | 3,617 | 3,617 | 2,487 | 1,130 | 1,018 |
| Mapping | 4,078 | 3,450 | 1,374 | 1,126 | 727 |
| CELLxGENE | 1,350 | 1,035 | 515 | 835 | 520 |
| Open Targets | 566,496 | 721,653 | 175,880 | 37,594 | 254,257 |
| Gene | 59,584 | 59,916 | 23,075 | 36,509 | 36,841 |
| UniProt | 32,256 | 27,278 | 4,238 | 28,018 | 23,040 |
| HuBMAP | 366 | 549 | 366 | 0 | 179 |

---

## NSForest (3,617 OLD / 3,617 NEW / 2,487 common)

Same total tuple count; differences are entirely in vertex annotations
and source handling.

### Attribute renames

| OLD | NEW |
|---|---|
| `#Binary_genes` | `#Markers` (same data, different annotation name) |

### Vertex annotations added in NEW

NEW emits additional CellSet schema attributes not present in OLD:
`#Author_cell_term`, `#Biomarker_combination`, `#Binary_gene_set`,
`#Expressed_genes`, `#Species`.

### Vertex annotations removed from NEW

OLD emitted `/rdf#type` and `/dc#Source` as vertex annotations (113
each); these are gone in NEW.

### Deduplication

OLD emitted `#F_beta_confidence_score` and `#Binary_genes` 226 times
each (doubled across CellSet and BiomarkerCombination); NEW emits each
113 times (once per cluster).

### Source predicate change

OLD used `/dc#Source` as a vertex annotation. NEW promotes it to a
relationship triple using the full `http://purl.org/dc/elements/1.1/source`
URI, moving 113 tuples from vertex_annotation to relationship.

---

## Mapping (4,078 OLD / 3,450 NEW / 1,374 common)

NEW produces fewer tuples overall, primarily through source
deduplication and restructuring annotations.

### Relationships

OLD emitted `BFO_0000050` (part_of) 1,018 times; NEW emits it only 64
times. The 954 difference corresponds to the OLD writer creating a
part_of triple for every gene-related tuple, while the NEW writer
creates it once per CellType-AnatomicalStructure pair.

NEW adds `http://purl.org/dc/elements/1.1/source` as a relationship
predicate (64 tuples), replacing the old source annotation pattern.

### Source deduplication

OLD emitted 1,943 source tuples (one per relationship). NEW emits
1,053 (deduplicated per association type).

### Vertex annotations dropped

OLD had 64 `#Cell_type` vertex annotations on CellSets. These are
removed in NEW.

### Vertex annotations added in NEW

NEW emits 16 distinct annotation attributes from the CellSet and
Publication schema entities:

- CellSet: `#Author_cell_term`, `#Ontology_purl`,
  `#Anatomical_structure`, `#Total_cell_count`,
  `#Biomarker_combination`, `#Binary_gene_set`, `#Expressed_genes`,
  `#Species`, `#Publication`, `#Cellxgene_collection`,
  `#Cellxgene_dataset`, `#Dataset_collection_version`, `#Collection_ID`
- Publication: `#Pmcid`, `#DOI`
- Shared: `#Label` (128 = 64 CellType + 64 AnatomicalStructure)

### Labels

NEW adds `#Label` annotations to CL and UBERON terms (e.g.,
`CL_0000129 -> "microglial cell"`), totaling 725 new vertex annotation
tuples.

---

## CELLxGENE (1,350 OLD / 1,035 NEW / 515 common)

### Attribute renames to match schema slot names

| OLD | NEW |
|---|---|
| `#Number_of_cells` | `#Total_cell_count` |
| `#Organism` | `#Species` |
| `#Tissue` | `#Anatomical_structure` |
| `#Link_to_CELLxGENE_dataset` | `#Cellxgene_dataset` |
| `#Link_to_CELLxGENE_collection` | `#Cellxgene_collection` |
| `/dc#Source` (vertex annotation) | `dc/elements/1.1/source` (relationship) |

### Attributes dropped in NEW

`#Dataset_ID`, `#Dataset_version_ID`, `#Collection_version_ID` (4
attributes x 75 = 300 tuples removed).

### Source predicate change

OLD used `dc#Source` as a vertex annotation; NEW promotes it to a
relationship triple and adds a source edge annotation with
`#Source = "CELLxGENE"`. NEW uses `PUB_NA` instead of a
dataset-specific publication ID when no DOI is available.

---

## Open Targets (566,496 OLD / 721,653 NEW / 175,880 common)

The largest change: +155K tuples, driven by new relationship types.

### New relationship predicates

| Predicate | OLD | NEW |
|---|---|---|
| `RO_0002435` (interacts_with) | -- | **111,484** |
| `RO_0002436` (molecularly_interacts_with) | 4,028 | **12,084** |

NEW adds `RO_0002435` (not present in OLD at all) and triples
`RO_0002436`. This accounts for ~117K of the 254K new-only tuples
(relationships + their source tuples).

### Attribute renames

| OLD | NEW |
|---|---|
| `#Indications` | dropped (was list of MONDO IDs) |
| `#Synonyms` | `#Exact_synonym` |
| `#Target` | `#Protein` |
| `#Genotype_ID` | `#Genotype_id` (case change) |
| `#Link_to_UniProt_ID` | `#Link_to_uniprot_id` (case change) |
| `#Link_to_PubChem_record` | `#Link_to_pubchem_record` (case change) |

### Attributes added in NEW

`#Approval_status`, `#Uniprot_id`, `#Label`, `#Definition`,
`#Gene_symbol`.

### Vertex annotation reduction

127,467 -> 43,544. Many old Drug annotations (`#Indications`,
`#Synonyms`, `#Target`, `#Trade_names`) were restructured into proper
relationships or renamed/consolidated.

---

## Gene (59,584 OLD / 59,916 NEW / 23,075 common)

Relationships and sources are identical (4,615 `RO_0003000` each). The
entire diff is vertex annotation renames.

### Attribute renames

| OLD | NEW |
|---|---|
| `#Official_full_name` | `#Label` |
| `#Official_symbol` | `#Gene_symbol` |
| `#Organism` | `#Species` |
| `#RefSeq_gene_ID` | `#Reference_sequence_identifier` |
| `#Summary` | `#Refseq_summary` |
| `#mRNA_(NM)_and_protein_(NP)_sequences` | `#Mrna__nm__and_protein__np__sequences` |
| `#Link_to_UniProt_ID` | `#Link_to_uniprot_id` (case change) |

### Value formatting

`#Also_known_as` changed from Python list repr (`"['A2MD', ...]"`) to
comma-separated (`"A2MD, ..."`).

### Tuple count difference (+332)

`#Also_known_as` went from 4,284 to 4,615 (now always emitted, even
when previously missing). The +332 net difference accounts for the
additional tuples.

---

## UniProt (32,256 OLD / 27,278 NEW / 4,238 common)

Purely vertex annotations in both. The 4,238 common tuples are
`#Function` values that did not change.

### Attribute renames

| OLD | NEW |
|---|---|
| `#Protein_name` | `#Label` |
| `#Gene_name` | `#Gene_symbol` |
| `#Organism` | `#Species` |
| `#UniProt_ID` | dropped (redundant with vertex key `PR_xxx`) |

### Value formatting

`#Annotation_score`: `"2.0"` -> `"2"` (float to int string).

### Null suppression

`#Function = "None"` no longer emitted; NEW drops null values instead
of serializing them as the string `"None"`. This removes 370 tuples
(4,608 - 4,238).

### Tuple count difference (-4,978)

`#UniProt_ID` dropped (-4,608) and `#Function` null suppression (-370).

---

## HuBMAP (366 OLD / 549 NEW / 366 common)

Perfect superset: every OLD tuple is present in NEW.

### Additions in NEW

183 new `#Label` vertex annotations on UBERON terms (e.g.,
`UBERON_0000052 -> "fornix"`). No other changes.

---

## Cross-cutting patterns

1. **Attribute names align to schema slot names**: `Organism` ->
   `Species`, `Official_symbol` -> `Gene_symbol`,
   `Protein_name`/`Official_full_name` -> `Label`, etc.

2. **Case normalization**: `#Link_to_UniProt_ID` ->
   `#Link_to_uniprot_id`, `#Link_to_PubChem_record` ->
   `#Link_to_pubchem_record`.

3. **List formatting**: Python list repr `"['A', 'B']"` ->
   comma-separated `"A, B"`.

4. **Source predicate promoted**: `dc#Source` vertex annotation ->
   `dc/elements/1.1/source` relationship triple (affects NSForest,
   Mapping, CELLxGENE).

5. **Null suppression**: NEW skips `"None"` values instead of emitting
   them as strings.

6. **Label annotations**: NEW consistently adds `#Label` annotations
   to ontology entities (CL, UBERON terms) across all writers.

7. **Open Targets structural change**: NEW adds `RO_0002435`
   (interacts_with) relationships -- this is the only case where the
   actual graph topology changed, not just annotation naming.

8. **URI warnings**: The Open Targets writer produces invalid URI
   warnings for disease names containing "NCT" (e.g., "allergic
   conjuNCT_ivitis", "erectile dysfuNCT_ion") -- the underscore
   replacement in `hyphenate()` is corrupting these terms.
