# Print/logging audit: python/src/*.py (top-level) and python/tests

Audit of every `print` and logging call in the Python worker scripts under
`python/src/*.py` (top-level, not `flows/`, not `_deprecated/`) and
`python/tests`, done 2026-09-28 as part of the read-only audit described in
[README.md](README.md), to inform the migration to the structured-logging
approach in `nlm-ckn-rnd/docs/proposals/logging-approach.md`. Read-only; no
code changes. Line numbers are as of `main` at `2d28ee3` and will drift as
fixes land.

Base path: `python/`.

**Not audited:** `flows/` (see [flows.md](flows.md)), `_deprecated/`, and
`OpenTargetsGGetQueries.py` and `ProductionDataSpecification.py` (no
prints).

**Line numbers:** InducedGraphBuilder line numbers are +/-1.

**Logging in the codebase today:**
- There is no `logging` module use except DataFetcher.py:686
  (`logging.getLogger(__name__).warning`).
- DataFetcher.py:227 uses `warnings.warn`.
- Everything else is `print`.
- No test asserts on stdout or logs (no capsys, caplog or assertLogs).
  Migrating is safe for tests.
- The only test print is tests/ArangoDbUtilitiesTestCase.py:11,
  `print(sys.path)`, which is DROP.

Message names below are proposed `snake_case` fixed strings. "Swallow" means
the code catches an error, prints, and carries on.

## src/DataFetcher.py (33 sites)

| Line | Current text | Verdict |
|---|---|---|
| 144 | `[src] 429 for id, retry a/N after Ns` | KEEP WARN `fetch_rate_limited` (source, attempt, max_retries, wait_s; drop id or keep it as a field) |
| 192 | `All N IDs already fetched` | MERGE into `fetch_plan` |
| 195 | `N already fetched, M remaining` | MERGE into `fetch_plan` INFO (source, ids_total, ids_cached, ids_pending) |
| 224 | `Error fetching id: exc` | KEEP WARN `fetch_item_failed` (source, id, error_type, error). **Swallow**: `on_fetch_error` fallback is stored as a result. |
| 227 | `warnings.warn(Unexpected error...)` | KEEP ERROR `fetch_item_unexpected_error` with exc_info. Not a log call today. **Broad except swallow.** |
| 236 | `Completed x/y - a/b total` | DEMOTE DEBUG `fetch_batch_done` (n_ok, n_failed). It currently counts attempts (`n_completed` is incremented before the try at 214). |
| 258 | `Resuming from checkpoint path` | KEEP INFO `fetch_checkpoint_resumed` (source, path, records_loaded) |
| 267 | `Merging with prior results` | MERGE into 258 |
| 277 | `Loading results from path` | MERGE into `fetch_cache_loaded` (source, path, records_loaded) |
| 291 | `Appending N records to ckpt` | DEMOTE DEBUG. `len(batch)` includes fallback results. |
| 308 | `Writing final results` | MERGE into the per-source `fetch_finished` line (see GAPS) |
| 313 | `Removed checkpoint` | DROP |
| 410 | `Assigning resources for gene` | DROP (per item, and logged on success) |
| 417 | `Could not assign resources for gene` | MERGE into `records_rejected` (source=opentargets). This is a fallback that stores empty resources. |
| 459 | `Fetching gene XML for id` | DROP (per item) |
| 473 | `Could not assign gene data` | MERGE into `records_rejected` (source=gene) |
| 534 | `Assigning results for protein` | DROP (per item) |
| 591 | `HuBMAP table already exists` | MERGE into the HuBMAP summary (n_present) |
| 599 | `Archived HuBMAP table` | MERGE into the HuBMAP summary (n_archived, versions) |
| 602 | `Removed HuBMAP table` | KEEP WARN `hubmap_table_removed` (path, reason). The bare `except Exception:` at 600 turns a failed archive into `os.remove`, which is data deletion with no error logged. |
| 608 | `Downloaded HuBMAP table` | MERGE into the HuBMAP summary (n_downloaded, organ, version, bytes) |
| 610 | `Could not download` | KEEP ERROR `hubmap_download_failed` (organ, version, status_code, url). **Swallow**: the run still finishes with `{}` and status "ok". |
| 668 | `WARNING: URLs could not be resolved` | KEEP WARN `hubmap_urls_unresolved` (n_failed, failures list). Should be a real level. |
| 675 | `WARNING: No HuBMAP URLs configured` | KEEP WARN `hubmap_no_urls_configured` |
| 686 | `logging.warning(Could not read fetch status)` | KEEP WARN `fetch_status_unreadable` (path, error). **Broad except** resets freshness state to `{}`. |
| 758 | `WARNING: Could not read results directory` | KEEP ERROR `results_dir_unreadable` (results_dir, error). **Swallow**: empty path lists mean nothing gets fetched. |
| 771 | `WARNING: Failed to build dataset version ID lists` | KEEP ERROR `dataset_ids_failed`. **Swallow** (`[]`). |
| 777 | `WARNING: No results_ensg_*.csv found` | KEEP ERROR `no_nsforest_results` (results_dir). This is an empty-input condition. |
| 787 | `WARNING: Failed to build gene data context` | KEEP ERROR `gene_context_failed` (error). **Swallow** (`gene_data=None`). |
| 814 | `Skipping - last success Xh ago` | KEEP INFO `fetch_skipped_fresh` (source, age_hours, max_age_hours) |
| 836 | `[src] exc; using cached results` | KEEP WARN `fetch_using_cache` (source, reason, records_loaded) |
| 841 | `ERROR: requires missing context` | KEEP ERROR `fetch_context_missing` (source, error). Should be a real level. |
| 859 | `ERROR: Fetcher failed and will be skipped` | KEEP ERROR `fetch_failed` (source, error, exc_info). **Broad except swallow**, though `main()` re-raises at 866. |

**Update (PR #103, plus its CodeRabbit follow-up):** the caching-failures-as-
successes bug this table's "Swallow" notes describe (224, 236) is fixed.
Fetch failures are now tracked in a `<name>.failures` sidecar, retried up to
`MAX_FETCH_ATTEMPTS`, and a source with retryable failures is reported
`partial` rather than `ok`. A `partial` outcome now also bypasses the
freshness-skip check (`_is_freshness_skip`), and the sidecar write is
atomic. This changes some of the fields available at 236 and around
811-824 but not the level/message-shape verdicts above.

## src/LoaderUtilities.py (34 sites, ignoring commented-out prints)

| Line | Current text | Verdict |
|---|---|---|
| 153 | `Invalid Ontology ID: 'GOREL'` | DEMOTE DEBUG. Per term; the caller keeps an unmapped count. |
| 158 | `Did not match ontology id or number` | DEMOTE DEBUG (same as 153) |
| 301 | `Warning: no root term in file` | KEEP WARN `uberon_root_missing` (path). **Swallow**: the file is skipped. |
| 337 | `Warning: no UBERON root term for organ` | KEEP WARN `uberon_root_missing` (organ). Dedupe per organ. |
| 352 | `tissue term X not among terms of organ` | MERGE into one line per organ (n_terms_unknown) |
| 373 | `dataset summary has no organ` | KEEP WARN `summary_organ_missing` |
| 467 | `WARNING: No master_dataset_summary found` | KEEP WARN `summary_missing` (results_file). Should be a real level. |
| 487 | `WARNING: X listed in manifest but not found` | KEEP WARN `results_missing_vs_manifest` (file, results_dir). Should be a real level. |
| 492 | `WARNING: Could not validate against manifest` | KEEP WARN `manifest_check_failed`. **Broad except swallow.** |
| 605 | `Loading NSForest results from path` | MERGE into one `gene_names_collected` (n_files, records_out) |
| 769 | `Add UUID column to results CSV` | KEEP INFO `results_uuid_added` (file, rows). **It mutates the input CSV in place.** |
| 822 | `Loading gene mapping from path (Nh old)` | KEEP INFO `gene_mapping_loaded` (source=local, path, age_hours, records_out) |
| 830 | `Local gene mapping is Nh old - re-fetching` | MERGE into `gene_mapping_loaded` (reason) |
| 847 | `Loaded gene mapping from s3://...` | KEEP INFO `gene_mapping_loaded` (source=s3, uri, age_hours) |
| 856 | `Gene mapping cache is Nh old` | MERGE |
| 861 | `WARNING: S3 credential error` | KEEP WARN `gene_mapping_s3_error`. **Swallow** (falls back). |
| 867 | `WARNING: S3 head_object failed` | KEEP WARN `gene_mapping_s3_error`. **Swallow.** |
| 872 | `Gene mapping not in S3` | MERGE into the `gene_mapping_source` decision line |
| 877 | `Getting names/ids from BioMart` | MERGE into `biomart_fetched` (records_out, duration_ms) |
| 894 | `BioMart query failed (attempt a/n)` | KEEP WARN `biomart_retry` (attempt, max, delay_s, error) |
| 911 | `Cached gene mapping to s3` | KEEP INFO `gene_mapping_cached` |
| 913 | `WARNING: Failed to cache gene mapping` | KEEP WARN `gene_mapping_cache_failed`. **Broad except swallow.** |
| 932 | `Creating gene name to Ensembl map` | DROP (re-runs the loader on every call) |
| 961 | `Could not find gene Ensembl ids for name` | MERGE into `gene_ids_collected` (records_rejected, sample). It is per item, thousands of lines. |
| 978 | `Creating Ensembl id to names map` | DROP |
| 1007 | `Could not find gene names for Ensembl id` | MERGE (per item) |
| 1024 | `Creating name to Entrez map` | DROP |
| 1053 | `Could not find Entrez ids for name` | MERGE (per item) |
| 1070 | `Creating Entrez id to names map` | DROP |
| 1099 | `Could not find gene names for Entrez id` | MERGE (per item) |
| 1322 | `Collected N Ensembl ids for M names` | KEEP INFO `gene_ids_collected` (kind=ensembl, records_in, records_out, records_rejected) |
| 1371 | `Collected N Entrez ids for M names` | KEEP INFO `gene_ids_collected` (kind=entrez, ...) |
| 1390 | `Creating EFO to MONDO map` | DROP (real fact is the path and row count, see GAPS) |
| 1512 | `Creating ChEMBL to PubChem map` | DROP |

## src/TupleWriterPipeline.py (27 sites)

| Line | Current text | Verdict |
|---|---|---|
| 35-37, 41-43, 47-49, 53-55, 59-61, 65-67 | `=`x70 banners and `Running X tuple writer` (6 writers) | DROP all 18 (banners/Running noise; blank `print()` on 40, 46, 52, 58, 64 also DROP) |
| 70, 73 | blank and closing banner | DROP |
| 72 | `All tuple writers complete` | KEEP INFO `tuple_writers_finished` (duration_ms, per-writer tuples_out). **Misleading**: each writer `return`s silently on missing input, so this prints "complete" even when nothing was written. |

**Update (PR #104):** the Gene, UniProt, CELLxGENE and Open Targets writers
now raise `FileNotFoundError` on missing input instead of returning silently,
so line 72's "complete" claim is no longer misleading in that case. The
`if tuples:` empty-output branches (a different case — input present,
nothing produced) are unchanged; see the GAPS section.

## src/InducedGraphBuilder.py (23 sites)

| Line | Current text | Verdict |
|---|---|---|
| 235 | `Adding hierarchy paths for prefix (strategy)` | MERGE into `hierarchy_enriched` per prefix (prefix, strategy, nodes_added, edges_added) |
| 259-261 | `After hierarchy enrichment: Vertices/Edges` | MERGE into the summary line |
| 289 | `Loading full graph via NetworkX` | MERGE into `source_graph_loaded` (duration_ms) |
| 293-294 | `Full graph vertices/edges` | MERGE into `source_graph_loaded` (vertices, edges) |
| 297 | `Finding all reachable vertices` | DROP |
| 299 | `Source vertices: N` | MERGE (source_vertices field) |
| 303 | `Source: <id>` | DROP (per source vertex, could be thousands) |
| 310 | `Extracting induced subgraph` | DROP |
| 313 | `Reachable vertices` | KEEP INFO `induced_subgraph_extracted` (records_in=full vertices, records_out, max_depth) |
| 314 | `Induced edges` | MERGE into 313 |
| 317 | `Adding ontology hierarchy paths` | DROP (duplicate of 235) |
| 321 | `Identifying collections` | DROP |
| ~334 | `Creating collections` | DROP |
| ~341 | `Inserting vertices` | MERGE into `vertices_inserted` (records_in, records_out, skipped_existing, duration_ms) |
| ~351 | `Inserting edges` | MERGE into `edges_inserted` |
| ~362 | `Warning: edge has no recognized collection, skipping` | KEEP WARN `edge_skipped` (edge_key). **Swallow**: silent data loss. Aggregate to a count. |
| ~374 | `Registering named graph` | DROP |
| ~389 | `Named graph created in database` | KEEP INFO `induced_graph_created` (graph, database, duration_ms) |
| ~390-391 | vertex/edge collection lists | MERGE into 389 |

## src/ArangoDbUtilities.py (23 sites)

| Line | Current text | Verdict |
|---|---|---|
| 43 | `Creating ArangoDB database` | KEEP INFO `arango_database_created` |
| 47 | `Getting ArangoDB database` | DROP (fires on every helper call) |
| 70 | `Deleting ArangoDB database` | KEEP INFO `arango_database_deleted` (destructive) |
| 91 | `Creating database graph` | KEEP INFO `arango_graph_created` |
| 94 | `Getting database graph` | DROP |
| 116 | `Deleting database graph` | KEEP INFO `arango_graph_deleted` |
| 137 | `Creating graph vertex collection` | DEMOTE DEBUG (dozens of lines per run) |
| 140 | `Getting graph vertex collection` | DROP |
| 162 | `Deleting vertex collection` | KEEP INFO |
| 189 | `Creating edge definition` | DEMOTE DEBUG |
| 196 | `Getting edge collection` | DROP |
| 218 | `Deleting edge definition` | KEEP INFO |
| 249, 256, 259, 266 | `Vertex/Edge collections:` headers and `TOTAL` lines | MERGE into `database_summary` (database, vertex_total, edge_total) |
| 255, 265 | per-collection `name  count` | KEEP INFO `collection_counted` (database, collection, kind, records_out). It reads true `.count()`; it is the reconciliation signal against records written by each source. |
| 438 | `Skipping view link for 'X': collection not found` | KEEP WARN `view_link_skipped` (collection, database). Silently narrows search. |
| 499-502 | blank, name, blank, `pprint(vertex)` | DEMOTE DEBUG `vertex_example` (collection, doc) |

Note `print_summary` builds its dict correctly; only the presentation
changes.

## src/OpenTargetsTupleWriter.py (10 sites)

| Line | Current text | Verdict |
|---|---|---|
| 65 | `MONDO term deprecated` | MERGE into `records_rejected` (reason=deprecated_term). It is a silent filter per disease. |
| 116 | `Cannot map Ensembl ID to gene name` | MERGE (reason=unmapped_ensembl) |
| 121 | `Cannot map gene name to Entrez ID` | MERGE (reason=unmapped_entrez) |
| 283 | `Missing variant RS ID` | MERGE (reason=missing_rsid) |
| 290 | `SO term deprecated` | MERGE. **Warns but still emits the tuple**, unlike 65 which drops. |
| 349 | `Missing drug ID in pharmacogenetics` | MERGE (reason=missing_drug_id) |
| 384, 387, 390 | `... results not found at path` | KEEP ERROR `input_missing` (input, path). **Swallow**: `return`, exit 0, downstream sees no opentargets.json. |
| 393 | `Creating Open Targets tuples from path` | KEEP INFO `tuple_writer_started` (source, input_path, records_in) |

**Update (PR #104):** 384/387/390 now raise `FileNotFoundError` (message
text unchanged) instead of printing and returning.

## src/E_Utilities.py (10 sites; a library module used by GeneFetcher)

| Line | Current text | Verdict |
|---|---|---|
| 147 | `Getting data for PMID` | DROP (per item) |
| 183 | `Error fetching from PubMed: status` | KEEP WARN `pubmed_fetch_failed` (pmid, status_code). **Swallow**: returns `{}`. |
| 208 | `Searching Gene for name` | DROP |
| 232 | `Found gene id X for name` | DROP |
| 235 | `No gene id found for name` | KEEP WARN `gene_search_empty` (name) |
| 238 | `Error searching Gene: status` | KEEP WARN `gene_search_failed` (name, status_code). **Swallow.** |
| 258 | `Fetching XML for gene id` | DROP |
| 273 | `Error fetching from Gene: status` | KEEP WARN `gene_fetch_failed` (gene_id, status_code). **Swallow**: returns None. The gene id is missing from the message. |
| 412-413 | `main()` demo prints | DROP |

## src/DataTransformer.py (6 sites)

| Line | Current text | Verdict |
|---|---|---|
| 66 | `Loading raw results from path` | MERGE into `transform_finished` (input_path) |
| 78 | `Saving transformed results to path` | MERGE (output_path) |
| 112 | `Output is up to date, skipping` | KEEP INFO `transform_skipped_up_to_date` (source, input_mtime, output_mtime) |
| 147 | `Skipping dataset_version_id (missing data)` | MERGE into `records_rejected` (reason=missing_dataset_or_collection). Per item. |
| 331 | `WARNING: skipping gene X: exc` | KEEP WARN `gene_parse_failed` (gene_id, error). **Broad except swallow** (writes `{}`). |
| 455 | `Transformed N entries` | KEEP INFO `transform_finished` (records_in, records_out, records_rejected, duration_ms). The count includes the meta key (`gene_entrez_ids` etc.) and empty `{}` entries, so it overstates. |

## src/MappingTupleWriter.py (5 sites)

| Line | Current text | Verdict |
|---|---|---|
| 101 | `No cell ontology ID for cluster` | MERGE into `records_rejected` (reason=no_cl_id) |
| 108 | `CL CURIE unexpected` | MERGE (reason=bad_cl_curie) |
| 113 | `CL term deprecated` | MERGE. **Warns but continues.** |
| 146 | `UBERON term deprecated` | MERGE. **Warns but continues.** |
| 293 | `Creating mapping tuples from file` | KEEP INFO `tuple_writer_dataset` (file, records_in, records_rejected, tuples_out) |

## src/UniProtIdMapper.py (4 sites)

| Line | Current text | Verdict |
|---|---|---|
| 43 | `print(response.json())` on HTTPError | KEEP ERROR `uniprot_http_error` (url, status_code, body[:500]). It re-raises, so it is not swallowed, but `response.json()` can itself raise on a non-JSON body and hide the HTTPError. |
| 124 | `Retrying in 1.5s` | DEMOTE DEBUG (polling) |
| 313 | `Fetched n / total` | DEMOTE DEBUG (paging progress) |
| 414 | `print(results)` in `main()` demo | DROP |

## src/GeneTupleWriter.py (4 sites)

| Line | Current text | Verdict |
|---|---|---|
| 53 | `No data for gene Entrez ID` | MERGE into `records_rejected` (reason=no_data). These are the failed-fetch `{}` entries. |
| 58 | `Cannot map gene Entrez ID to name` | MERGE (reason=unmapped_name) |
| 117 | `Gene results not found at path` | KEEP ERROR `input_missing`. **Swallow** (`return`). |
| 120 | `Creating Gene tuples from path` | KEEP INFO `tuple_writer_started` |

**Update (PR #104):** 117 now raises `FileNotFoundError` instead of
printing and returning.

## src/UniProtTupleWriter.py (3 sites)

| Line | Current text | Verdict |
|---|---|---|
| 45 | `No data for protein accession` | MERGE into `records_rejected` (reason=no_data) |
| 77 | `UniProt results not found` | KEEP ERROR `input_missing`. **Swallow.** |
| 80 | `Creating UniProt tuples from path` | KEEP INFO `tuple_writer_started` |

**Update (PR #104):** 77 now raises `FileNotFoundError` instead of printing
and returning.

## src/ProductionDataValidator.py (3 sites)

| Line | Current text | Verdict |
|---|---|---|
| 697 | per-finding `SEVERITY check: msg [path]` | KEEP as one event per finding, level mapped from ERROR/WARN, message fixed `validation_finding` (check, path, detail) |
| 698 | `N error(s), M warning(s) in dir` | KEEP INFO `validation_finished` (n_errors, n_warnings, data_dir, exit_code, duration_ms) |
| 728 | `print(json.dumps(...))` for `--json` | KEEP. This is machine output, not a log. Keep it on stdout and route logs to stderr. |

## src/TupleWriterUtilities.py (2 sites)

| Line | Current text | Verdict |
|---|---|---|
| 161 | `Warning: Could not parse string list` | KEEP WARN `string_list_parse_failed` (value[:80], error). **Swallow**: returns `[]`, so a row loses all markers or binary genes. Aggregate the count. |
| 770 | `Wrote N tuples to path` | KEEP INFO `tuples_written` (path, records_out, deduped_removed, duration_ms) |

## src/CellxGeneTupleWriter.py (2 sites) and src/NSForestTupleWriter.py (1 site)

| Line | Current text | Verdict |
|---|---|---|
| CellxGene 147 | `CELLxGENE results not found` | KEEP ERROR `input_missing`. **Swallow** (`return`). |
| CellxGene 150 | `Creating CELLxGENE tuples from path` | KEEP INFO `tuple_writer_started` |
| NSForest 600 | `Creating NSForest tuples from file` | KEEP INFO `tuple_writer_dataset` (file, records_in, records_rejected, tuples_out) |

**Update (PR #104):** CellxGene 147 now raises `FileNotFoundError` instead
of printing and returning.

## Totals (191 sites)

| Verdict | Count |
|---|---|
| KEEP | 71 |
| MERGE | 53 |
| DEMOTE | 12 |
| DROP | 55 |

## GAPS: messages that should exist but don't

**DataFetcher.py**
- **run() 159-246, no per-source finish line.** A `fetch_finished` line per
  source is missing: ids_total, records_in=len(pending), records_out=n_ok,
  records_rejected=n_failed, n_cached, duration_ms.
- **Failed IDs are cached as successes.** Lines 216-234 store the
  `on_fetch_error` fallback (`{}` or empty resources) in `results` and in
  the JSONL checkpoint. On the next run, line 188 treats those IDs as
  already fetched. `n_completed` (214) and `len(batch)` (236, 291) count
  attempted IDs, not successes. Keep `n_ok`/`n_failed` per batch and per
  source, plus a sample of failed IDs. **Fixed in PR #103** (retry cap,
  `.failures` sidecar, `partial` outcome) and its CodeRabbit follow-up
  (atomic sidecar writes, fail-closed on an unreadable sidecar, freshness
  bypass for `partial`).
- **Open Targets needs per-source accumulation.** Each batch should add to
  running ok/failed/empty-target totals. `fetch_one` can also return a 200
  with `target: null` or a GraphQL `errors` key. Line 413 does `["data"]`
  and never checks for these, so a real "no data" looks like a success.
  Count empty targets separately from errors.
- **Rate-limit retries (136-152).**
  - There is no log when retries are exhausted.
  - After the loop, a final `fetch_one` at 152 raises outside any try.
  - The 429 path only fires for `requests.HTTPError`.
    `E_Utilities.fetch_xml_for_gene_id` (267-274) returns None on any
    non-200, including 429. GeneFetcher raises `RuntimeError` at 462 with no
    retry, and the status code is lost.
- **UniProtFetcher.get_ids 509-514.** Genes with empty data or no
  `UniProt_name` are silently dropped. Log accessions_in,
  genes_without_accession and the unique count.
- **_load 264 and 271.** `json.loads` on each checkpoint line has no try. A
  line truncated by a crash kills resume without a message.
- **Status masking.** 855-856 records `last_outcome="ok"` even when HuBMAP
  downloads failed (610) or 100% of items hit `on_fetch_error`.
- **HuBMAP.**
  - 604-610 has no exception handling on the download, so a request error
    aborts the source.
  - 596-602 is a broad except followed by `os.remove`.
  - The resolved version `json_ver` (664) and organ are never logged.
- **main() 707-869, no script start or finish.** Nothing is logged at start
  (run_name, run_dir, flags, `--max-source-age-hours`) or at finish
  (per-source outcomes, duration_ms). Only a raised `RuntimeError` (866)
  marks failure.
- **Context built at 753-799 has no counts.** Missing: nsforest_files,
  dataset_ids, gene_names, ensembl_ids, entrez_ids.
- **Skip-fresh path 821-824.** The number of cached records loaded is not
  logged.
- **Input versions and paths.** Not logged: Open Targets API endpoint and
  data release, the UniProt release header, and the NCBI email/API-key
  presence (E_Utilities.py:12-13 controls the rate tier).

**LoaderUtilities.py**
- **Protein ID mapping, lines 1131-1147 and 1210-1226.** These two
  functions look unused outside their definitions; verify before relying on
  the findings. Related silent-loss bugs if they are called:
  - The last-batch trigger `protein_id == protein_ids[-1]` never fires if
    the final id is not ENSP (or is ENSP for the reverse map), so the last
    batch is silently dropped.
  - If `check_id_mapping_results_ready` returns False, `data` is stale or
    unbound.
  - `failedIds` from UniProt is never read.
  - Nothing logs submitted, mapped or unmapped counts, the job id, or
    duration.
- **BioMart 882-890.** `.dropna().drop_duplicates()` silently drops rows.
  Log rows_fetched and rows_after_cleanup, the Ensembl/BioMart release, and
  duration_ms. 901-903 `.astype(int)` will crash on a non-integer id with no
  context. The final BioMart failure at 900 is only re-raised, not logged.
- **Cache reads (825, 851).** Rows loaded are not logged.
- **collect_unique_gene_names 1276-1282.** The `clusterSize >=
  MIN_CLUSTER_SIZE` filter (10) silently drops clusters, and
  `ast.literal_eval` has no try.
- **collect_unique_gene_ensembl_ids/entrez_ids 1309-1370.**
  - Unmapped names are silently discarded (only the per-item prints).
  - Multi-id cases take `[0]` with no ambiguity count.
  - 1358-1361 sets `gene_name=None` on an unmapped Ensembl id, then does a
    lookup with None.
- **load_results 767-772.** It rewrites the input CSV, with the index
  written as a column. No row or column counts are logged, and none of the
  four `read_csv` sites (294, 530, 574, 767) log row counts.
- **get_dataset_file_paths 423-458.**
  - There is no count of nsforest files or companion files per kind. Sparse
    mapping and binary files are missing by design, but the counts would
    show it.
  - The raise at 537 ("No dataset version id") is not logged as a
    structured event.
- **get_dataset_organs_map 575-582.** Skips summaries without a
  `dataset_version_id` column and rows with a NaN id silently.
- **get_uberon_root_map 292-303.** No count of files, organs or roots.
- **Reference data.**
  - EFO to MONDO (1394): the CSV path and row count are not logged, and
    unmapped EFO returns None silently at 1418.
  - src1src22.csv (1513): path and row count not logged.
  - MONDO mesh2mondo (1438-1474): the OBO file path and version, the entry
    count and the four hardcoded overrides are not logged.
- **Also.** `get_values_or_none` (1580-1587) raises TypeError if a value is
  None. `main()` (1591-1603) has no start or finish line.

**Tuple writers**
- **No `if tuples:` else branch.** NSForest 597, Mapping 297, Gene 125,
  UniProt 85, OpenTargets 402 and CellxGene 151 all skip the write when
  tuples is empty, with no message. (This is the empty-*output* case, and is
  unaffected by PR #104's missing-*input* fix.)
- **Silent filters and skips.**
  - **NSForestTupleWriter:**
    - 202-203 skips the whole dataset when no cluster passes the size
      filter.
    - 273-274 drops clusters below `MIN_CLUSTER_SIZE`.
    - 125 gives a None mean_binary_score.
    - The silhouette merge at 536-540 is an inner join with no unmatched
      count.
    - The loop at 500-600 has no per-dataset records_in/out.
  - **MappingTupleWriter:**
    - 262-263 skips results with no mapping file.
    - 90-91 drops small clusters.
    - The merge at 277-289 is an inner join, so unmatched cluster names
      vanish.
    - 118-123 gives an empty `summary_row` when the dataset_version_id does
      not match.
  - **OpenTargetsTupleWriter:**
    - 141 (score < 0.5)
    - 176 (phase not in PHASE_3/APPROVAL)
    - 178-181 (withdrawn)
    - 236-240 (indication phase)
    - 261 (non-NCT report)
    - 213 (no UniProt accession, so no
      DrugMolecularlyInteractsWithProtein edge)
    - 131 (missing gene_results entry)
    - 140 and 176 use hard `[...]` access and will KeyError on malformed
      rows with no context.
  - **GeneTupleWriter:** 89-103 does not count genes with vs without a
    protein association.
  - **CellxGeneTupleWriter:** `year=str(metadata.get("Year"))` writes the
    string "None" when the year is missing.
  - **TupleWriterUtilities.py:753-770:**
    `_dedupe_annotation_triples_last_wins` removes triples silently. Log
    before and after counts.
- **TupleWriterPipeline.py:38-68.** There is no try/except or per-writer
  timing. It could not tell "writer skipped for missing input" from "wrote
  N tuples" before PR #104; the missing-input case now raises. Per-writer
  timing is still missing.

**DataTransformer.py**
- **Empty results.** Empty `{}` entries at 319-321, 363-365 and 283-286 are
  counted silently as transformed.
- **Input handling.**
  - `input_path` missing raises `FileNotFoundError` uncaught (67).
  - Staleness is by mtime only (54-56), and the mtimes are not logged.
  - 205 `dataset_json["assets"][0]["url"]` will KeyError with no context.
- **Versions.** No raw input file version is logged.

**InducedGraphBuilder.py**
- **Destructive setup, no log.**
  - 411-413 deletes the whole phenotype database.
  - 419-420 deletes the graph.
  - Environment variables default to `""` and are never validated
    (395-402).
- **Insert counts.** 345 and 367 skip existing documents silently. Log
  inserted and skipped counts.
- **BFS filters.** The ignored-collection and self-referential skips
  (71-95) and the `MAX_DEPTH=10` truncation are not counted.
- **Empty source.** 298-299 does not warn when zero source vertices are
  found, so the result is an empty graph.
- **Failure handling.** There is no try/except, so a failure mid-insert
  leaves a partial database with no failure line. `data["_key"]` (367) can
  KeyError.
- **Timing and lifecycle.** No duration_ms on load (290), BFS (301-304) or
  inserts. No script start or finish line.

**ArangoDbUtilities.py**
- **Analyzers and views.**
  - create_analyzers (284-324), create_view (443) and delete_analyzers/
    delete_view (340-342, 464) log nothing.
  - Not logged: the collection_maps path (363), how many view links were
    built, or that 411-431 silently prunes links for `Cell-KN-Phenotypes`.
- **Crash.** 497 `randint(0, count-1)` crashes on an empty collection.
- **Connection.** Host/port and database are not logged on connect
  (`_client`, 8-24).

**UniProtIdMapper.py**
- **Errors and retries.**
  - `failedIds` is combined at 180 but never reported.
  - 127 raises `Exception(jobStatus)` without the job id.
  - The `Retry(total=5)` adapter (18-20) logs nothing when it retries.
- **Result size.** `x-total-results` (350) is not logged as records_out.
- **Version.** The UniProt release header is not logged.

**ProductionDataValidator.py**
- **Bare except blocks.** These swallow errors and continue silently:
  - 432
  - 477 (`continue`)
  - 498 (`continue`)
  - 511
  - 562
  - 611
  - 657
  - 109 returns the exception object instead of raising
  - 336 and 630 do record findings
- **Missing lifecycle lines.** No validation start line. No per-check
  duration or files_checked count in `validate()` (680-689).

**Cross-cutting**
- **Script lifecycle.** None of these scripts logs start, finish or failure
  with duration_ms and the resolved run: DataFetcher.main,
  DataTransformer.main (428-455), TupleWriterPipeline.main, each writer's
  `main()`, InducedGraphBuilder `__main__`, LoaderUtilities.main and
  ProductionDataValidator.main.
- **Phase tagging.** No phase field (fetch/ontology/results/archive) exists
  anywhere. The phase should be bound once (per script) via structlog
  contextvars.
- **Failure signalling.** Missing-input paths `return` with exit 0 in every
  tuple writer. **Partially fixed in PR #104** (Gene, UniProt, CELLxGENE,
  Open Targets now raise); NSForest and Mapping were not in scope for that
  fix.

## Top 10 highest-value gaps

1. **DataFetcher.py:216-234 and 188.** `on_fetch_error` fallbacks are
   stored as results and checkpointed, then treated as "already fetched" on
   resume. Add `records_out` (n_ok), `records_rejected` (n_failed), a
   failed-ID sample, and stop caching failures as successes. **Fixed in PR
   #103** and its CodeRabbit follow-up.
2. **DataFetcher.py:236-241, 214, 291.** Counters count attempted IDs, not
   successes. Add a per-source `fetch_finished` line with counts and
   duration_ms, with Open Targets batches accumulated per source.
3. **DataFetcher.py:855-856 and 610.** Status "ok" is written when HuBMAP
   downloads fail or every item fell back to an error result. Derive the
   outcome from the counts. **Partially addressed by PR #103**: a source
   with retryable fetch failures is now `partial`, not `ok`. The HuBMAP-
   specific download-failure masking (610, independent of the retry
   mechanism) is unchanged.
4. **Tuple writers' `return` on missing input** (OpenTargets 384-390, Gene
   117, UniProt 77, CellxGene 147, and the empty `if tuples:` branches).
   These exit 0 silently, and TupleWriterPipeline.py:72 then reports
   success. Log ERROR `input_missing`, log `tuples_out`, and fail or report
   skipped. **The four named writers' missing-input case fixed in PR
   #104**; the empty-`if tuples:` output case is unchanged.
5. **NSForest and Mapping writers' silent drops.** In
   NSForestTupleWriter.py:202-203, 273-274 and 536-540, and in
   MappingTupleWriter.py:90-91, 262-263 and 277-289 (inner merges), clusters
   and datasets disappear without a count. Add one line per source with
   records_in/out/rejected and a reason.
6. **Gene ID mapping loss in LoaderUtilities.py:1309-1370.** Thousands of
   per-item prints now, with no aggregate. Log unmapped counts
   (`records_rejected`) and ambiguous counts (first-of-many) at 1322/1371.
7. **BioMart and S3 gene mapping (LoaderUtilities.py:882-917).** Log rows
   before and after `.dropna().drop_duplicates()`, the BioMart release, and
   duration_ms. Also log which cache tier was used and the age.
8. **Open Targets tuple filters (OpenTargetsTupleWriter.py:141, 176-181,
   236-240).** Score, phase and withdrawn filters, and deprecated terms,
   drop records silently or by warn-and-continue. Emit counts per reason.
9. **InducedGraphBuilder.py:411-420 and inserts (345, 367).** Silent
   database deletion, skipped existing documents, and no failure or timing
   log for a multi-step destructive build. Add start, finish, failure,
   duration_ms, and inserted vs skipped counts.
10. **Resolved input versions and paths never logged.** Not logged: the
    results_dir and file list (LoaderUtilities.py:423), HuBMAP json_ver
    (DataFetcher.py:664), Open Targets and UniProt releases, the MONDO OBO
    file, EFO/ChEMBL mapping CSV paths and row counts, and the
    BioMart/Ensembl release. A run cannot be reproduced or attributed
    without them.
