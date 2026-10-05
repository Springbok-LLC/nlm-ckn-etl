# Logging conventions

The vocabulary every log line in this repo draws from: the `message` names, the
`reason` values, and the field names. Java, the Python scripts and the Prefect
flows all use it, so a query written against one surface works on the others.

The schema itself (core fields, correlation, CloudWatch wiring) is in
`nlm-ckn-rnd/docs/proposals/logging-approach.md`. The sites that this
vocabulary replaces are listed in [logging-audit/](logging-audit/README.md).
Where the audit reports proposed a spaced phrase (`"arangodb ready"`), the
name here is the `snake_case` form (`arangodb_ready`).

## 1. Rules for `message`

1. **`snake_case`, fixed, low-cardinality.** Lowercase letters, digits and
   underscores only. No ids, counts, paths, versions or punctuation in the
   string; those are fields. `stats count() by message` must be meaningful.
2. **One name per kind of event, on every surface.** A Java class and a Python
   script that both write tuples emit `tuples_written`. Do not add a
   surface-specific variant.
3. **`<subject>_<event>`**, with the event as a past participle or a fixed
   suffix:

   | Suffix | Meaning |
   | --- | --- |
   | `_started` | A unit of work began. Always paired with a `_finished` or `_failed`. |
   | `_finished` | The unit completed. Carries `duration_ms` and counts. |
   | `_failed` | The unit did not complete. Carries `error_type` and `error` when an exception caused it; otherwise a `reason` (section 5) and the measurement, `rc` or `status_code`. |
   | `_skipped` | The unit was deliberately not run. Carries `reason`. |
   | `_loaded`, `_written`, `_created`, ... | A completed action with a result. |

4. **Name the outcome, not the step.** Log `vertices_inserted` once with counts,
   not `inserting_vertices` followed by `vertices_inserted`.
5. **Per-item events become one count.** Thousands of "could not map gene X"
   lines become one summary line with `records_rejected` and a `reason`
   (section 5). Log the first few items at DEBUG with an id field.
6. **A new name goes in the tables below in the same PR that first emits it.**
   Reuse an existing name when the meaning matches.

## 2. Registries

### `service`

| Value | Deployable |
| --- | --- |
| `etl-pipeline` | Prefect pipeline flow and the Java and Python worker processes it launches |
| `etl-fetcher` | Prefect fetch flow and its Python workers |
| `ui-backend` | The Django backend (not covered by this repo's audit) |

Add a value here before using it, so `ckn-ui` never appears beside `nlm-ckn-ui`.

### `phase`

Bound once per scope. One of `fetch`, `ontology`, `results`, `archive`. The
release flow's own orchestration lines carry `step` (1, 2 or 3) and no
`phase`; lines from the flows it calls carry their own phase.

### `source`

The fetcher name: `cellxgene`, `opentargets`, `gene`, `uniprot`, `hubmap`.
Transform and tuple-writer lines reuse the same value, plus `nsforest` and
`mapping` for the writers that have no fetcher.

### `outcome`

For `_finished` lines that summarize a source or run: `ok`, `partial`,
`failed`, `skipped`, `cached`. These match `last_outcome` in
`fetch-status.json`.

## 3. Fields

Tier 3 payload fields. Use these names and units; do not invent synonyms.

| Field | Type | Meaning |
| --- | --- | --- |
| `records_in` | int | Arrived at this boundary |
| `records_out` | int | Passed this boundary |
| `records_rejected` | int | Dropped here. Pair with `reason`. |
| `duration_ms` | int | Wall time of the unit, in milliseconds. Never "seconds" text or `_s`. |
| `source_version` | string | The resolved upstream version (Open Targets `26.03`, UniProt `2026_01`) |
| `reason` | string | A value from section 5 |
| `error_type` | string | Exception class name |
| `error` | string | `str(exception)`; the traceback goes in the `exception` field |
| `rc` | int | Subprocess return code |
| `bytes` | int | Size transferred or written |
| `path`, `input_path`, `output_path` | string | File paths |
| `database`, `graph`, `collection` | string | ArangoDB names |
| `attempt`, `max_retries`, `wait_s` | int, int, float | Retry state |
| `artifact` | string | `tuples`, `baseline_dump`, `results_dump`, `golden`, `obo`, `external` |
| `release_tag`, `run_name` | string | Release identity |

Counts are JSON numbers, never strings. Lists are fields, never message text.
Never log secrets: log whether a variable is set, not its value.

## 4. Levels

| Level | Use |
| --- | --- |
| `ERROR` | The unit failed, or data was lost. Every raise on a failure path is preceded by one `*_failed` ERROR that meets the `_failed` contract in section 1. |
| `WARN` | Degraded but continuing: a fallback was used, a source returned partial data, a retry happened, or a destructive step ran. |
| `INFO` | One line per completed unit of work. The default view of a run. |
| `DEBUG` | Per-item detail and progress. Off in production. |

A `*_started` line is INFO only at phase, flow and subprocess boundaries;
elsewhere drop it and log the `_finished` line.

## 5. `reason` values

For `records_rejected` and `*_skipped` and `*_failed` lines. Lowercase
`snake_case`. Add to this table, do not free-text.

| Reason | Where it applies |
| --- | --- |
| `no_data` | A fetched entry was empty, so it produces no tuples (gene, protein) |
| `deprecated_term` | An ontology term (MONDO, CL, UBERON, SO) is deprecated |
| `unmapped_ensembl` | An Ensembl id has no gene name |
| `unmapped_entrez` | A gene name has no Entrez id |
| `unmapped_name` | An Entrez id has no gene name |
| `missing_rsid` | A variant has no RS id |
| `missing_drug_id` | A pharmacogenetics row has no drug id |
| `missing_dataset_or_collection` | A dataset version has no dataset or collection |
| `no_cl_id` | A cluster has no Cell Ontology id |
| `bad_cl_curie` | A Cell Ontology CURIE has an unexpected form |
| `invalid_vertex` | A tuple subject or object is not a valid vertex |
| `invalid_edge_label` | An edge predicate label is null or filtered |
| `not_a_triple` | A tuple has neither 3 nor 5 elements |
| `blank_node` | A triple contains a blank node |
| `parse_failed` | A value could not be parsed (string list, gene record) |
| `unrecognized_collection` | An edge has no known ArangoDB collection |
| `dangling_edge` | An edge references a vertex that was not loaded |
| `dump_exists` | A phase was skipped because its dump is already present |
| `up_to_date` | Output is newer than input |
| `fresh` | A fetch was skipped because the last success is recent |
| `no_marker` | No fetch-info marker exists, so resume |
| `code_changed` | The fetch code hash differs from the marker |
| `expired` | The marker is older than the allowed age |
| `invalid_marker` | The marker is missing or malformed |
| `empty` | A cache file is empty |
| `missing_sentinel` | A cache file lacks its sentinel key |
| `not_found` | An expected input or artifact is absent |
| `rate_limited` | The upstream returned HTTP 429 |
| `http_error` | The upstream returned a non-success status; add `status_code` |
| `nonzero_exit` | A subprocess exited with a non-zero `rc` |

## 6. Message vocabulary

Names below are the contract. The fields listed are the ones a line carries
in addition to the core fields and the common ones in section 3. The audit
reports give the source line each name replaces.

### Run and phase lifecycle (every surface)

| Message | Level | Fields |
| --- | --- | --- |
| `run_started` | INFO | `main_class` or `script`, effective config (`run_name`, dirs), `args` |
| `run_finished` | INFO | `outcome`, `duration_ms`, summary counts |
| `run_failed` | ERROR | `error_type`, `error`, `duration_ms` |
| `config_resolved` | INFO | Once per flow: `run_name`, `release_tag`, `s3_enabled`, phase flags, `java_opts`, `jar_key`, `git_commit`, `fetch_code_hash` |
| `phase_started` | INFO | `phase` |
| `phase_finished` | INFO | `phase`, `duration_ms`, counts |
| `phase_skipped` | INFO | `phase`, `reason`, `force` |
| `subprocess_started` | INFO | `phase`, `script` or `main_class`, `java_opts` |
| `subprocess_finished` | INFO | `phase`, `rc`, `duration_ms` |
| `subprocess_failed` | ERROR | `phase`, `reason=nonzero_exit`, `rc`, `duration_ms`; note `rc=137` as out of memory |
| `source_versions_resolved` | INFO | `source`, `source_version` per source |

### Fetch (`DataFetcher`, `fetch.py`, `_common.py`)

| Message | Level | Fields |
| --- | --- | --- |
| `fetch_plan` | INFO | `source`, `ids_total`, `ids_cached`, `ids_pending` |
| `fetch_finished` | INFO | `source`, `source_version`, `outcome`, `records_in`, `records_out`, `records_rejected`, `duration_ms`; Open Targets accumulates across batches first |
| `fetch_batch_done` | DEBUG | `source`, `n_ok`, `n_failed` |
| `fetch_skipped_fresh` | INFO | `source`, `age_hours`, `max_age_hours` |
| `fetch_using_cache` | WARN | `source`, `reason`, `records_loaded` |
| `fetch_rate_limited` | WARN | `source`, `attempt`, `max_retries`, `wait_s` |
| `fetch_item_failed` | WARN | `source`, `id`, `error_type`, `error` |
| `fetch_item_unexpected_error` | ERROR | `source`, `id`, `error_type`, `error` |
| `fetch_failed` | ERROR | `source`, `error_type`, `error` |
| `fetch_context_missing` | ERROR | `source`, `error` |
| `fetch_checkpoint_resumed` | INFO | `source`, `path`, `records_loaded` |
| `fetch_cache_loaded` | INFO | `source`, `path`, `records_loaded` |
| `fetch_status_unreadable` | WARN | `path`, `error` |
| `fetch_cache_decision` | INFO | `decision` (`force` or `resume`), `reason`, `age_hours`, `threshold_hours`, `cached_hash`, `current_hash` |
| `fetch_info_written` | INFO | `validated`, `files_ok`, `files_missing`, `commit` |
| `fetch_info_unreadable` | WARN | `source` (`s3` or `local`), `error` |
| `cache_file_unreadable` | WARN | `file`, `error` |
| `cache_file_removed` | WARN | `file`, `reason` |
| `cache_entries_cleared` | INFO | `records_out`, `files_touched` |
| `external_source_empty` | ERROR | `file`, `source` |
| `external_files_validated` | INFO | `files_ok`, `bytes` |
| `hubmap_download_failed` | ERROR | `organ`, `version`, `reason=http_error`, `status_code`, `url` |
| `hubmap_table_removed` | WARN | `path`, `reason` |
| `hubmap_urls_unresolved` | WARN | `n_failed`, `failures` |
| `hubmap_no_urls_configured` | WARN | |
| `results_dir_unreadable` | ERROR | `results_dir`, `error` |
| `no_nsforest_results` | ERROR | `results_dir` |
| `dataset_ids_failed` | ERROR | `error_type`, `error` |
| `gene_context_failed` | ERROR | `error_type`, `error` |
| `pubmed_fetch_failed` | WARN | `pmid`, `reason=http_error`, `status_code` |
| `gene_search_empty` | WARN | `name` |
| `gene_search_failed` | WARN | `name`, `reason=http_error`, `status_code` |
| `gene_fetch_failed` | WARN | `gene_id`, `reason=http_error`, `status_code` |
| `uniprot_http_error` | ERROR | `url`, `status_code`, `body` |

### Transform and tuple writers

| Message | Level | Fields |
| --- | --- | --- |
| `transform_finished` | INFO | `source`, `input_path`, `output_path`, `records_in`, `records_out`, `records_rejected`, `duration_ms` |
| `transform_skipped_up_to_date` | INFO | `source`, `input_mtime`, `output_mtime` |
| `gene_parse_failed` | WARN | `gene_id`, `error_type`, `error` |
| `tuple_writer_started` | INFO | `source`, `input_path`, `records_in` |
| `tuple_writer_dataset` | INFO | `file`, `records_in`, `records_rejected`, `tuples_out` |
| `tuple_writers_finished` | INFO | `duration_ms`, `tuples_out` per writer |
| `input_missing` | ERROR | `input`, `path` (the writer then raises) |
| `tuples_written` | INFO | `output_path`, `records_out`, `records_rejected`, `deduped_removed`, `duration_ms` |
| `records_rejected` | WARN | `source`, `reason`, `records_rejected`, `sample` (a short list of ids) |
| `string_list_parse_failed` | WARN | `value`, `error_type`, `error`; aggregate to a count |
| `gene_mapping_loaded` | INFO | `source` (`local`, `s3`, `biomart`), `path` or `uri`, `age_hours`, `records_out`, `reason` |
| `gene_mapping_cached` | INFO | `uri` |
| `gene_mapping_cache_failed` | WARN | `error_type`, `error` |
| `gene_mapping_s3_error` | WARN | `error` |
| `biomart_retry` | WARN | `attempt`, `max_retries`, `wait_s`, `error` |
| `gene_ids_collected` | INFO | `kind` (`ensembl`, `entrez`), `records_in`, `records_out`, `records_rejected` |
| `gene_names_collected` | INFO | `n_files`, `records_out` |
| `uberon_root_missing` | WARN | `path` or `organ` |
| `summary_organ_missing` | WARN | |
| `summary_missing` | WARN | `results_file` |
| `results_missing_vs_manifest` | WARN | `file`, `results_dir` |
| `manifest_check_failed` | WARN | `error_type`, `error` |
| `results_uuid_added` | INFO | `file`, `rows` |

### Ontology and graph load (Java and Python)

| Message | Level | Fields |
| --- | --- | --- |
| `ontology_downloaded` | INFO | `ontology`, `version_new`, `url`, `status_code`, `bytes`, `duration_ms` |
| `ontology_updated` | INFO | `ontology`, `version_prev`, `version_new`, `archived_to` |
| `ontology_unchanged` | INFO | `ontology`, `version_cur`, `version_new` |
| `ontology_version_not_found` | WARN | `file` |
| `ontologies_downloaded` | INFO | `records_out`, `duration_ms` |
| `ontology_slimmed` | INFO | `input_file`, `output_file`, `mode`, `records_out`, `records_rejected`, `duration_ms` |
| `ontology_file_archived` | INFO | `from`, `to` |
| `taxon_lineages_built` | INFO | `taxa_count`, `permitted_taxa_count` |
| `ontology_files_not_found` | ERROR | `pattern` |
| `ontology_elements_parsed` | INFO | `records_in` (files), `records_out` (terms), `duration_ms` |
| `triples_collected` | INFO | `file`, `records_in`, `records_out`, `records_rejected`, `duration_ms` |
| `unique_triples_collected` | INFO | `files`, `records_in`, `records_out`, `duration_ms` |
| `triples_skipped` | WARN | `records_rejected`, `reason` |
| `vertices_constructed` | INFO | `records_in`, `records_out`, `records_rejected`, `duration_ms` |
| `vertices_updated` | INFO | `records_in`, `records_out`, `duration_ms` |
| `vertices_inserted` | INFO | `collection`, `records_in`, `records_out`, `records_rejected`, `skipped_existing`, `duration_ms` |
| `edges_constructed` | INFO | `records_in`, `records_out`, `records_rejected`, `duration_ms` |
| `edges_updated` | INFO | `records_in`, `records_out`, `duration_ms` |
| `edges_inserted` | INFO | `collection`, `records_in`, `records_out`, `records_rejected`, `duration_ms` |
| `edges_dropped_by_label_filter` | WARN | `edge_collection`, `records_rejected`, `label_counts` |
| `insert_failed` | ERROR | `collection`, `key`, `error_type`, `error`; first few only, then the summary |
| `edge_skipped` | WARN | `records_rejected`, `reason=unrecognized_collection` |
| `tuples_file_processed` | INFO | `tuples_file`, `records_in`, `records_rejected`, `duration_ms` |
| `tuples_files_not_found` | ERROR | `pattern`, `dir` |
| `graph_loaded` | INFO | `graph`, `records_out`, `edges`, `records_rejected`, `duration_ms` |
| `source_graph_loaded` | INFO | `vertices`, `edges`, `duration_ms` |
| `hierarchy_enriched` | INFO | `prefix`, `strategy`, `vertices_added`, `edges_added`, `duration_ms` |
| `induced_subgraph_extracted` | INFO | `records_in`, `source_vertices`, `reachable_vertices`, `records_out`, `max_depth`, `duration_ms` |
| `induced_graph_created` | INFO | `graph`, `database`, `vertices_written`, `edges_written`, `duration_ms` |
| `aql_paths_collected` | INFO | `query_id`, `records_out`, `duration_ms` |

### ArangoDB administration

| Message | Level | Fields |
| --- | --- | --- |
| `arangodb_container_started` | INFO | `port`, `volume_kind`, `duration_ms` |
| `arangodb_container_reused` | INFO | `container_id`, `port` |
| `arangodb_container_removed` | INFO | `container_id`, `status` |
| `arangodb_ready` | INFO | `port`, `duration_ms` |
| `arangodb_endpoint_resolved` | INFO | `mode`, `host`, `port`, `container_id` |
| `arangodb_data_wiped` | INFO | `kind`, `volume` or `path` |
| `arangodb_volume_removal_failed` | WARN | `volume`, `error_type`, `error` |
| `docker_unreachable` | WARN | |
| `arangodump_finished` | INFO | `dump_label`, `file_count`, `bytes`, `rc`, `duration_ms` |
| `arangorestore_finished` | INFO | `dump_label`, `rc`, `duration_ms` |
| `database_created` | INFO | `database` |
| `database_dropped` | WARN | `database` |
| `graph_created` | INFO | `database`, `graph` |
| `graph_dropped` | WARN | `database`, `graph` |
| `graph_recreated` | INFO | `database`, `graph` |
| `graph_already_present` | INFO | `database`, `graph` |
| `graph_recreate_failed` | ERROR | `database`, `graph`, `reason=http_error`, `status_code` |
| `graph_sidecars_exported` | INFO | `database`, `graph_count`, `analyzer_count` |
| `analyzers_and_views_created` | INFO | `database`, `duration_ms` |
| `view_link_skipped` | WARN | `collection`, `database` |
| `collection_counted` | INFO | `database`, `collection`, `kind`, `records_out` (read back from `.count()`) |
| `database_summary` | INFO | `database`, `vertex_total`, `edge_total` |

### Artifacts, S3 and release

| Message | Level | Fields |
| --- | --- | --- |
| `jar_resolved` | INFO | `source` (`cache`, `maven`, `s3`), `bytes`, `jar_key`, `duration_ms` |
| `s3_sync_finished` | INFO | `direction` (`up`, `down`), `s3_uri`, `objects_transferred`, `objects_skipped`, `bytes`, `duration_ms` |
| `s3_upload_finished` | INFO | `artifact`, `s3_uri`, `bytes`, `files`, `duration_ms` |
| `s3_download_finished` | INFO | `artifact`, `s3_uri`, `bytes`, `duration_ms` |
| `artifact_cache_hit` | INFO | `artifact`, `path` |
| `artifact_upload_skipped` | WARN | `artifact`, `reason` |
| `external_cache_promoted` | INFO | `objects_copied`, `src_prefix`, `duration_ms`; WARN when 0 |
| `release_dir_validated` | INFO | `records_out` (NSForest files) |
| `tuple_files_validated` | INFO | `records_out` (files) |
| `production_promotion_finished` | INFO | `run_name`, `objects`, `bytes`, `duration_ms` |
| `release_started` | INFO | `release_tag`, `run_name`, `github_repo`, `tar_source`, `max_fetch_age_hours`, `java_opts`, `hubmap_url_count` |
| `release_tarball_fetched` | INFO | `source_kind` (`https`, `s3`, `local`), `uri` or `path`, `bytes`, `duration_ms` |
| `release_tarball_extracted` | INFO | `nsforest_files`, `hubmap_urls`, `manifest_rows`, `files_skipped`, `duration_ms` |
| `release_manifest_merged` | INFO | `records_out` |
| `results_promoted_to_latest` | INFO | `objects_copied`; WARN when 0 |
| `release_config_saved` | INFO | `path` |
| `release_step_failed` | ERROR | `step`, `error_type`, `error`, `retry_command` |
| `release_promotion_failed` | ERROR | `release_tag`, `error_type`, `error` |
| `release_finished` | INFO | `release_tag`, `run_name`, `duration_ms`, `force_fetch`, per-step durations |
| `github_status_posted` | INFO | `state`, `status_code` |
| `github_status_skipped` | WARN | `missing_vars` |
| `github_status_failed` | WARN | `state`, `reason=http_error`, `status_code` |
| `arango_password_fetch_failed` | ERROR | `secret_id`, `error_type`, `error` |

### Validation

| Message | Level | Fields |
| --- | --- | --- |
| `validation_finding` | ERROR or WARN, by finding severity | `check`, `path`, `detail` |
| `validation_finished` | INFO | `n_errors`, `n_warnings`, `data_dir`, `exit_code`, `duration_ms` |

`ProductionDataValidator --json` is machine output, not a log: keep it on
stdout and send log lines to stderr for that invocation.

## 7. Open points

- `PhenotypeGraphBuilder` is `@Deprecated` and is not covered. Its audit rows
  would map onto the `vertices_inserted` and `edges_inserted` names if it is
  ever revived.
- The UI (`ui-backend`) vocabulary (`request_finished` and friends) belongs to
  the `nlm-ckn-ui` repo and is not defined here.
- Per-source `source_version` values for fetch lines come from
  `fetch-status.json` and `fetch-info.json`; phase 2 wires that read-back.
