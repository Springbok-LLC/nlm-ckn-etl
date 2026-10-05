# Logger audit: python/src/flows/ (read-only, no changes made)

Audit of every logger call in the Prefect flows under `python/src/flows/`,
done 2026-09-28 as part of the read-only audit described in
[README.md](README.md), to inform the migration to the structured-logging
approach in `nlm-ckn-rnd/docs/proposals/logging-approach.md`. Read-only; no
code changes. Line numbers are as of `main` at `2d28ee3` and will drift as
fixes land.

Base path: `python/src/flows/`.

## Facts that change the plan

- **Actual call counts.** `pipeline.py` has 91. `fetch.py` has 15.
  `_common.py` has 31 (25 direct plus 6 routed through the `log` callback in
  `should_force_fetch`). `release.py` has 22 plus 1 `print` at
  `release.py:556`, so 23 in total. That is 160 anchors.
- **No Batch submit in `release.py`.** The only Batch-related code is
  `_common.py:391-402` (`_cloudwatch_log_url`). It reads
  `AWS_BATCH_JOB_ID`, but the URL points at the log group only, with no
  stream and no job id. Its result goes only into the GitHub deployment
  payload and is never logged.
- **The subprocess helper logs nothing.** `_run_python_script`
  (`_common.py:193-233`) and the five `subprocess.run` Java calls in
  `pipeline.py` (419, 460, 479, 1018, 1040) log nothing on their own. Start
  and finish lines are written by the callers as f-strings, and only
  "success" is ever logged. rc, duration, and the JVM options are never
  captured.
- **Subprocess output bypasses the logger.** `subprocess.run` inherits
  stdout and stderr. `log_prints=True` is set on every flow and task, but
  that only captures Python `print`, not child output. Under structlog,
  child output will be interleaved raw text. It needs capturing and
  forwarding, or a documented separation.
- **`arangodump` and `arangorestore` output is discarded on success.**
  `exec_run` output is kept only when the exit code is non-zero
  (`pipeline.py:531-534`, `761-764`).
- **`README.md` documents no log expectations.** It says nothing about log
  lines, levels, or the CloudWatch log group. Its only operator-facing
  guarantees are that `runs/latest` is never updated on failure, and the
  retry commands. Those retry commands are duplicated in
  `release.py:413-490` `logger.error` text. It is also stale: it says Phase
  2 builds the induced-subgraph, but the code does that in Phase 3.
- **Two logging paths.** `_common.py` uses stdlib `_log` for non-task
  helpers, and tasks use `get_run_logger()`. `should_force_fetch` takes
  `log=` (defaulting to `_log.info`), and both flows pass `logger.info`.
  That hides level and structure, so the callback must be replaced with a
  returned decision object or a bound logger.

## `pipeline.py` (91 calls)

Proposed fields are in parentheses. Rows with a range collapse repeated
patterns.

| Line(s) | Current text | Verdict |
|---|---|---|
| 137 | "Docker daemon unreachable — nothing to stop" | RELEVEL to WARN, "docker unreachable" |
| 141 | "No ArangoDB container found" | DEMOTE (debug) |
| 144 | "Stopping/removing container {id} (status)" | KEEP INFO, "arangodb container removed" (container_id, status) |
| 168, 178 | "Removed named volume" / "Wiping data dir" | KEEP INFO, "arangodb data wiped" (volume or path, kind) |
| 170 | "Named volume not found…" | DEMOTE |
| 174 | "Could not remove named volume" | KEEP WARN, "arangodb volume removal failed" (volume, error). **Log-and-continue**: a stale volume keeps the old password and causes a later 401. |
| 268 | "ArangoDB container already running" | KEEP INFO, "arangodb container reused" (container_id, port). Bare `except: pass` at 275 falls back to the default port silently. |
| 282, 288, 300 | "Starting…(volume/home, port)" and "started on port" | MERGE into one "arangodb container started" (port, volume_kind, duration_ms) |
| 305 | "ArangoDB is accepting connections" | KEEP INFO, "arangodb ready" (port, duration_ms of the wait) |
| 321, 331 | "Remote ArangoDB mode…" / "Local ArangoDB container" | MERGE into one "arangodb endpoint" (mode, host, port, container_id) |
| 357, 361 | "JAR already present (size)" / "JAR key" | MERGE into "jar resolved" (source=cache, bytes, jar_key) |
| 365 | "building JAR locally with Maven" | KEEP INFO, "jar build started" |
| 374 | "JAR built … key" | KEEP INFO, "jar resolved" (source=maven, bytes, jar_key, duration_ms). The Maven subprocess at 366 has no rc or duration line. |
| 381 | "Downloading JAR from s3" | DROP (386 carries the result) |
| 386 | "JAR downloaded … key" | KEEP INFO, "jar resolved" (source=s3, bytes, jar_key, duration_ms) |
| 418, 459, 476 | "Downloading / Slimming / Building ontology graph (class, opts)" | DROP. Replace with a single subprocess start line (main_class, java_opts, phase). |
| 430 | "Downloaded {n} OWL file(s)" | KEEP INFO, "ontologies downloaded" (records_out=n, duration_ms) |
| 466, 485 | "Ontologies slimmed" / "Ontology graph built" | DROP, replaced by subprocess exit (rc, duration_ms) |
| 515 | "Dumping ArangoDB → dir (label)" | DROP, merged into 550 |
| 550 | "Dump complete (label): n files" | KEEP INFO, "arangodump complete" (dump_label, file_count, bytes, duration_ms). Add the dump command's own output at debug. |
| 612, 623 | "Exported {n} graph(s)" / "analyzer(s)" | MERGE into "graph sidecars exported" (db, graph_count, analyzer_count) |
| 614, 627 | "Could not export graphs / analyzers" (WARN) | RELEVEL to ERROR. **Log-and-continue.** The code comments say a missing sidecar starves InducedSubgraph of ontology edges, so this should fail the run or at least be ERROR. **Fixed in PR #105**: both now raise `RuntimeError` after attempting every database. |
| 696 | "Recreated graph db/name" | KEEP INFO, "graph recreated" (db, graph) |
| 699 | "Graph already exists — skipping" | KEEP INFO, "graph already present" (db, graph) |
| 702 | "Could not recreate graph" (WARN) | RELEVEL to ERROR, "graph recreate failed" (db, graph, http_status). **Log-and-continue**, same downstream impact as 614. **Fixed in PR #105**: raises `RuntimeError` (an existing graph, HTTP 409, is still not treated as a failure). |
| 705 | "Graph sidecar import complete" | MERGE, becomes the summary line (created, skipped, failed counts) |
| 733 | "Restoring ArangoDB from dir" | DROP (765 carries it) |
| 765 | "ArangoDB restore complete" | KEEP INFO, "arangorestore complete" (dump_label, duration_ms) |
| 808 | "Release dir OK: n NSForest files" | KEEP INFO, "release dir validated" (records_out=n) |
| 835, 882, 924, 942, 970, 990, 1126 | "S3_BUCKET not set — skipping … (local mode)" (7 copies) | DROP. One DEBUG "s3 disabled" in the flow-start config line. |
| 841, 860, 865 | "Syncing … →" / "Writing all tuples" / "All tuples written" | DROP. 843 and the subprocess exit line carry the information. |
| 843 | "Release dir synced from S3" | KEEP INFO, "s3 sync complete" (direction=down, s3_uri, objects_transferred, objects_skipped, bytes, duration_ms) |
| 887, 889 | "Compressing and uploading tuples" / "Tuples pushed" | MERGE into "s3 upload complete" (artifact=tuples, s3_uri, bytes, files, duration_ms) |
| 911 | "Tuple files: n JSON file(s)" | KEEP INFO, "tuple files validated" (records_out=n). It should also log zero-row or size outliers per file. |
| 927, 929 | baseline upload (2 lines) | MERGE into "s3 upload complete" (artifact=baseline_dump, jar_key, bytes, duration_ms) |
| 945 | "Baseline dump already present locally" | KEEP INFO, "cache hit" (artifact=baseline_dump, jar_key, path) |
| 950, 954 | baseline download / restored | MERGE into "s3 download complete" (artifact=baseline_dump, s3_uri, bytes, duration_ms) |
| 973, 975 | results dump upload | MERGE, same pattern (artifact=results_dump, jar_key, run_name) |
| 993 | "Results dump already present locally" | KEEP INFO, "cache hit" (artifact=results_dump) |
| 998, 1003 | results dump download | MERGE, same pattern |
| 1015, 1024, 1037, 1053 | "Building results graph… / built", "Building induced subgraph… / built" | DROP, replaced by subprocess start and exit lines |
| 1079 | "Creating analyzers and views in db" | MERGE into 1087 |
| 1084 | "Nothing to delete in db" | DEMOTE to debug. **Bare `except Exception` swallows all errors** (network or auth as well as a missing view), and the message asserts "nothing to delete". |
| 1087 | "Analyzers and views created" | KEEP INFO, "analyzers and views created" (database, duration_ms) |
| 1138 | "Promoting artifacts to prefix" | DROP |
| 1142, 1149, 1160 | "Compressing and uploading golden / OBO / external" | MERGE. Each becomes one "s3 upload complete" (artifact, s3_uri, bytes, duration_ms) and replaces the log lines around 1143, 1150, 1161. |
| 1152, 1163 | "data/obo/ not found — skipping" / "external cache not found — skipping" | KEEP WARN, "artifact upload skipped" (artifact, reason). **Log-and-continue**: a silently incomplete production promotion. |
| 1205 | "Could not read fetch-info.json" | KEEP WARN, "fetch info unreadable". **Log-and-continue**, and the build-info file then loses provenance. Bare `except: pass` at 1211 and 1221 (pom and pyproject versions) also swallow silently. |
| 1240 | "All artifacts promoted to prefix" | KEEP INFO, "production promotion complete" (run_name, objects, bytes, duration_ms) |
| 1328 | "No stage flags set — nothing to do" (WARN, then `return` = success) | RELEVEL to ERROR, or raise. A misconfigured run reports success. **Fixed in PR #106**: raises `ValueError`. |
| 1340, 1342 | "S3 mode: bucket=" / "Local mode" | MERGE into one flow-start config line (see gaps) |
| 1368 | "Baseline dump already exists — use force_ontology" | KEEP INFO, "phase skipped" (phase=ontology, reason=dump_exists, jar_key, force=false) |
| 1373 | "=== Phase 1 … ===" | KEEP INFO, "phase started" (phase=ontology). Drop the banner formatting. |
| 1388 | "Remote ArangoDB … skipping container start/stop" | MERGE into the 321/331 endpoint line |
| 1410 | "Phase 1 complete — baseline dump" | KEEP INFO, "phase finished" (phase=ontology, jar_key, duration_ms, dump path) |
| 1421, 1429 | "Results dump / Golden dump already exists" | KEEP INFO, "phase skipped" (phase=results or archive, reason=dump_exists, force=false) |
| 1462, 1526 | Phase 2 banner and "Phase 2 complete" | KEEP INFO, "phase started" and "phase finished" (phase=results, duration_ms, records_in/out/rejected if available) |
| 1533, 1579 | Phase 3 banner and "Phase 3 complete" | KEEP INFO, same pattern (phase=archive) |

Not counted above: 1383 and 1456 only pass `logger` into
`_wipe_arangodb_data`.

## `fetch.py` (15 calls)

| Line | Current text | Verdict |
|---|---|---|
| 85 | "external dir does not exist — nothing to clean" | DEMOTE |
| 93 | "Skipping {file}: {exc}" | KEEP WARN, "cache file unreadable" (file, error). **Log-and-continue** on a corrupt cache. |
| 106 | "{file}: no empty entries" (per file) | DEMOTE |
| 112 | "{file}: removed n entries" | MERGE into 117 as a per-file field |
| 117 | "Total empty entries removed" | KEEP INFO, "failed cache entries cleared" (records_out=total, files_touched) |
| 151, 153 | "Force mode…" / "Source max age Nh" | MERGE into one config line (force, max_source_age_hours) |
| 156 | "Fetching external API results (DataFetcher)" | DROP (subprocess start line replaces it) |
| 173 | "External API results fetched" | DROP (replaced by subprocess exit with rc and duration_ms, phase=fetch) |
| 199, 210 | Transformer start and end | DROP (same, phase=fetch, script=DataTransformer.py) |
| 278 | "Fetch artifact written to path" | KEEP INFO, "fetch info written" (validated, files_ok, files_missing, commit) |
| 426, 428 | "S3 mode: bucket=" / "Local mode" | MERGE into flow-start config line |
| 433 | `should_force_fetch(..., logger.info)` callback | RELEVEL. Replace the callback with a structured decision (see the `_common.py` cache-decision rows below). |

Other issues in `fetch.py`:
- `fetch.py:265` and `286` are bare `except: pass` and `except Exception:
  pass`. The first hides a missing git commit. The second hides an
  unreadable `fetch-status.json`.
- `fetch.py:464-466` re-raises after recording the artifact but never logs
  a structured ERROR for a validation failure.
- The per-source outcomes in `fetch-status.json` (ok, failed, skipped) are
  surfaced only in the Prefect markdown artifact (`fetch.py:290-333`) and
  never logged.

**Update (PR #103, plus its CodeRabbit follow-up):** the fetch summary
table's outcome icons now include `partial`, and the freshness check that
`should_force_fetch`/line 433 feeds into is complemented by a separate
`_is_freshness_skip` decision in `DataFetcher.py` for the per-source retry
case; the flow-level `should_force_fetch` callback itself is unchanged and
still tracked as a gap here.

## `_common.py` (31 anchors)

| Line | Current text | Verdict |
|---|---|---|
| 128 | Secrets Manager failure (ERROR, then `raise`) | KEEP ERROR, "arango password fetch failed" (secret_id, error) |
| 422 | "GitHub deployment status: state repo deployment_id token" | DEMOTE to debug. Log only presence flags, never values. |
| 430 | "Skipping deployment status update — env vars missing" | KEEP WARN, "github status skipped" (missing_vars) |
| 456 | "GitHub deployment status posted: HTTP" | KEEP INFO, "github status posted" (state, http_status) |
| 458, 463 | "status update failed" (two variants) | MERGE into one WARN "github status failed" (state, http_status, error). **Log-and-continue by design**, and the docstring says so. |
| 551 | "Could not read fetch-info.json from S3" | KEEP WARN, "fetch info unreadable" (source=s3, error). Falls through to "resume". |
| 561 | "Could not parse fetch-info.json" (local) | KEEP WARN, same message (source=local, error) |
| 568, 576, 595, 601 | Four decision lines (none / hash changed / too old / reuse) | MERGE into one INFO "fetch cache decision" (decision=force or resume, reason=no_marker or code_changed or expired or fresh, age_hours, threshold_hours, cached_hash, current_hash, source=s3 or local). |
| 585 | "Missing/invalid fetched_at … resuming" | RELEVEL to WARN, and fold into the decision line as reason=invalid_marker |
| 654 | "Removed empty/corrupt external cache file" | KEEP WARN, "cache file removed" (file, reason=empty) |
| 655 | "Cleaned n empty file(s)" | KEEP INFO, "external cache cleaned" (records_out=n) |
| 657 | "No empty files found" | DEMOTE |
| 671 | "Removed …/file: missing sentinel key" | KEEP WARN, "cache file removed" (file, reason=missing_sentinel, key). The `except JSONDecodeError: pass` at 675 is silent. |
| 724 | "valid JSON but contains no entries — annotations skipped" | RELEVEL to ERROR or WARN with a metric. A required source that is empty is silent data loss. Fixed message "external source empty" (file, source). |
| 730 | "OK: file (bytes)" per file, 8 lines | MERGE into one INFO "external files validated" (files_ok, total_bytes, source=fetch or pipeline) |
| 761, 786, 819, 849 | "S3_BUCKET not set — skipping (local mode)" | DROP |
| 766, 790, 824, 853 | "Syncing … →" / "Promoting … →" | MERGE with the following result line |
| 768, 792, 826 | "External cache restored / pushed / pushed to staging" | KEEP INFO, "s3 sync complete" (direction, s3_uri, objects_transferred, objects_skipped, bytes, duration_ms). `_s3_sync` currently returns nothing, so the counts need a return value. |
| 857 | "Promoted n object(s) from staging" | KEEP INFO, "external cache promoted" (objects_copied, src_prefix, duration_ms). Log a WARN when the count is 0. |

`validate_external_files` raises `RuntimeError` at 737 with a multi-line
message but has no structured ERROR (see gaps). `_get_arangodb_id` returns
`None` on `DockerException` at 175-176 with no log.

## `release.py` (22 calls plus 1 print)

| Line | Current text | Verdict |
|---|---|---|
| 119, 138 | "Downloading release tarball: url" / "Downloading s3://…" | DROP (133 and 140 carry it) |
| 133, 140 | "Downloaded to {name}" | KEEP INFO, "release tarball fetched" (source_kind=https or s3, uri, bytes, duration_ms) |
| 147 | "Using local tarball: path" | KEEP INFO, same message (source_kind=local, path) |
| 161 | "Extracting → dir (flat)" | DROP |
| 199 | "Unioned n manifest rows" | KEEP INFO, "release manifest merged" (records_out=n) |
| 211, 225 | hubmap urls written / "Extracted n NSForest result files" | MERGE into "release tarball extracted" (nsforest_files, hubmap_urls, manifest_rows, files_skipped, duration_ms) |
| 245, 272 | "S3_BUCKET not set — skipping (local mode)" | DROP |
| 250, 277 | "Syncing / Promoting …" | MERGE with the result line |
| 252 | "Release dir pushed to S3" | KEEP INFO, "s3 sync complete" (objects, bytes, duration_ms) |
| 281 | "Promoted n result file(s)" | KEEP INFO, "results promoted to latest" (objects_copied). This is the atomicity boundary, so log a WARN on count 0. |
| 305 | `should_force_fetch(..., logger.info)` callback | RELEVEL. Use the structured decision (see `_common.py` 568-601). |
| 374 | "Release: tag= run=" | KEEP INFO, "release started" (tag, run_name, github_repo, tar_source, max_fetch_age_hours, java_opts, hubmap_url_count, release_config source) |
| 413, 441, 475 | Multi-line ERROR with retry instructions, using `%s` on 441 and 475 | RELEVEL. Keep ERROR, but use a fixed message, e.g. "release step failed" (step=1, 2 or 3, exception type, error, retry_command). The exception itself is never logged. It goes only into the GitHub status description, truncated to 140 characters. Also, retry hints belong in a field, not the message. |
| 497 | "Promotion of tag failed: exc" | KEEP ERROR, "release promotion failed" (tag, error) |
| 504 | "Release tag complete (run=)" | KEEP INFO, "release finished" (tag, run_name, duration_ms, force_fetch, per-step durations). The elapsed time is computed at 505-506 but sent only to GitHub. |
| 556 | `print("[release] Saved config to path")` | RELEVEL. Runs before the flow starts, so it has no Prefect run logger. Move to the structlog module logger, INFO "release config saved" (path). |

## GAPS: messages that should exist but don't

1. **Subprocess rc and duration (all launches).**
   - Sites: `_common.py:225-233` (`_run_python_script`), and
     `pipeline.py:366` (Maven), `419`, `460`, `479`, `1018`, `1040` (Java).
   - Nothing logs rc or duration_ms, and `check=True` raises a bare
     `CalledProcessError`. Add a start line (script or main_class, argv,
     java_opts, phase, cwd) and an exit line (rc, duration_ms, phase).
     Cover `OutOfMemoryError` and rc 137 explicitly, as the CLI help text
     (`pipeline.py:1648`) already advises.
   - Wrap in try/except and emit an ERROR "subprocess failed" before
     re-raising.
2. **Task and phase timing.**
   - No task logs duration_ms, and only the Phase banners at
     `pipeline.py:1373`, `1462`, `1533` open phases. Phase end has no
     duration. Fetch has no `phase=fetch` boundary at all
     (`fetch.py:436-469` runs the sync, fetch, transform, validate, and
     stage steps unlabelled).
3. **`arangodump` and `arangorestore`.**
   - Sites: `pipeline.py:520-534`, `745-764`.
   - Output is discarded on success. Log the command line (with the
     password masked), exit_code, duration_ms, and the dump directory size.
     Warn if the restore reports errors but exits 0.
4. **Flow-start config line, once per flow.**
   - Sites: `pipeline.py:1316`, `fetch.py:393`, `release.py:366`.
   - Log run_name, correlation_id, tag, s3_bucket presence, phase flags
     (run and force for each phase), java_opts, jar_key, git commit, and
     `fetch_code_hash`. Right now S3 mode is a single line that names the
     bucket.
5. **Resolved data-source versions.**
   - Sites: `fetch.py:445-457` and `_common.py:496-508`.
   - Open Targets release, UniProt release, NCBI Gene date, CELLxGENE
     census version, HuBMAP endpoint and each ontology OWL version are not
     logged by the flows. The Open Targets and UniProt versions are decided
     inside `DataFetcher.py`, which is out of scope. The flow should read
     them back from `fetch-status.json` or `fetch-info.json` and log them
     once, at the end of fetch and at `pipeline.py:1482-1483`, and again at
     flow start for pipeline.
   - The same applies to release config: `release.py:378` reads `cfg`, but
     only the hubmap URL count is used. `release_config`, `tar_source`, and
     `github_repo` are not logged.
5b. **Force and cache decisions with no source detail.**
   - Sites: `fetch.py:151-153` says "ignoring on-disk cache" without saying
     which sources (per-source ok, failed, or skipped) were skipped or
     re-fetched, or how old each was.
   - `release.py:425-431` logs the force decision only inside the GitHub
     description string.
   - `should_force_fetch` returns a bool with no reason or ages in
     structured form.
6. **S3 sync counts and bytes.**
   - Sites: `_common.py:331-385` (`_s3_sync`), `_common.py:243-273` and
     `276-306` (tar upload and download), `_common.py:309-328`
     (`_s3_copy_prefix`), and `pipeline.py:1233-1238` (`build-info.txt`
     upload).
   - `_s3_sync` and the tar helpers return nothing. Add
     objects_transferred, objects_skipped, and bytes. Log the S3 key and
     KMS presence, but not the KMS key id.
   - `_s3_upload_tar` silently drops `.archive/` members
     (`_common.py:267`). Log the skipped-file count.
   - `_s3_download_tar` silently skips symlinks and path-traversal members
     (`_common.py:298-303`). Log the skipped-member count as WARN.
7. **Broad excepts that swallow.**
   - Sites: `pipeline.py:275` (port lookup), `1084` (view delete), `1170`
     and `1211` and `1221` (build-info), `fetch.py:265` and `286`, and
     `_common.py:675`, and `_get_arangodb_id` at `_common.py:175`.
   - All silent, so log at least at DEBUG or WARN with the exception type.
8. **Failure paths that raise without a structured ERROR.**
   - Sites: `_common.py:737` (`validate_external_files` puts the list of
     failing files into the exception text only), `pipeline.py:429` (OWL
     empty), `1473` and `1548` (missing dumps), and `release.py:129` and
     `144` (tarball not found).
   - No flow wraps its body in a top-level try/except that logs a final
     ERROR with the failing phase, task, and elapsed time. Only
     `release.py` logs failure, and only at step level.
9. **Batch job id and log-stream link.**
   - Sites: `_common.py:391-402` (`_cloudwatch_log_url`) and
     `_common.py:405-463`.
   - There is no Batch submit call in this code, so there is no submit line
     to add. The URL builder points at the `/batch/nlm-ckn-release` log
     group without a stream. Log AWS_BATCH_JOB_ID, the Prefect flow-run id,
     and the log group and stream once at flow start.
10. **Run-level summary line at flow end.**
    - Sites: `fetch.py:469` (nothing logged at the end), `pipeline.py:1579`,
      `release.py:504`.
    - None emits counts. Add: fetch (sources ok, failed, skipped,
      records_out per source, rejected empties, total duration_ms),
      pipeline (per-phase duration, dump sizes, tuple counts, `records_in`
      and `records_out` and `records_rejected` for the graph loads),
      release (step durations, force flag, objects promoted). The pipeline
      currently logs nothing at all when all phases were skipped. **Update
      re: `pipeline.py:1328`:** all-phases-skipped now raises `ValueError`
      rather than logging nothing (PR #106), which is a step toward this
      but not the summary line itself.
11. **Counts that the Java and Python subprocesses know but the flows do
    not log.**
    - Sites: `pipeline.py:865` and `889` (TupleWriter), `1024` (results
      graph), `1053` (induced subgraph).
    - Only file counts (`pipeline.py:911`) are logged, with no tuple-row
      counts and no graph vertex or edge counts. Those belong to
      `records_in`, `records_out`, and `records_rejected`.
12. **Stale-cache and fetch-status visibility.**
    - Site: `fetch.py:281-287`. Per-source last_outcome, last_success_at,
      and age are collected for the artifact but never logged. One INFO or
      WARN line per source is needed for alerting.

## Totals by verdict (160 anchors)

| File | KEEP | DROP | MERGE | DEMOTE | RELEVEL | Total |
|---|---|---|---|---|---|---|
| `pipeline.py` | 39 | 23 | 21 | 3 | 5 | 91 |
| `fetch.py` | 3 | 4 | 5 | 2 | 1 | 15 |
| `_common.py` | 12 | 4 | 11 | 2 | 2 | 31 |
| `release.py` | 10 | 5 | 3 | 0 | 5 | 23 |
| **All** | **64** | **36** | **40** | **7** | **13** | **160** |

Roughly 40 percent of lines are droppable or mergeable pass-through. After
the merges, about 100 lines remain. The 4 `MERGE` clusters in
`should_force_fetch` and the 2 pairs of `S3 sync` lines are the largest
consolidations.

## Top 10 highest-value gaps

1. **Subprocess start and exit with rc and duration_ms** on every Python
   and Java launch (`_common.py:225`, `pipeline.py:366`, `419`, `460`,
   `479`, `1018`, `1040`). This is the natural `phase` boundary and the
   biggest diagnostic hole. Also capture or forward child stdout and stderr
   so it correlates with the flow-run id.
2. **A final structured ERROR on every failure path** (task and flow
   level: failing phase, task, rc, exception type, duration_ms), and stop
   passing the exception only to the GitHub status text (`release.py:413`,
   `441`, `475`, `_common.py:737`, `pipeline.py:429`).
3. **Phase and task duration_ms plus a `phase=fetch` boundary.** Phase
   banners exist only in the pipeline, and none carry a duration
   (`pipeline.py:1373`, `1462`, `1533`; `fetch.py:436-469`).
4. **Silent data-loss warnings to ERROR or raise:** graph and analyzer
   sidecar failures (`pipeline.py:614`, `627`, `702`), empty required
   source (`_common.py:724`), promotion skips (`pipeline.py:1152`, `1163`,
   `1205`), and the no-stage-flags return-as-success (`pipeline.py:1328`).
   **The sidecar failures and no-stage-flags items are fixed** (PR #105,
   PR #106); the empty-required-source and promotion-skip items are
   unchanged.
5. **Structured cache and force decision** in place of the four free-text
   `should_force_fetch` lines and the `log=` callback
   (`_common.py:511-605`, `fetch.py:433`, `release.py:305`, `425-431`),
   with reason, ages, and hashes, and per-source status from
   `fetch-status.json`.
6. **S3 sync, tar, and copy counts and bytes** (`_common.py:331`, `243`,
   `276`, `309`, `pipeline.py:1233`), including skipped files.
7. **Flow-start config and resolved-version line** (git commit,
   `fetch_code_hash`, jar_key, run_name, tag, tar_source, s3_bucket
   presence, phase flags, java_opts, resolved Open Targets and UniProt
   versions), replacing `pipeline.py:1340-1342`, `fetch.py:426-428`,
   `release.py:374`.
8. **Run-level summary line at the end of each flow** (`fetch.py:469`,
   `pipeline.py:1579`, `release.py:504`), including the elapsed time
   currently sent only to GitHub (`release.py:505-506`).
9. **`arangodump` and `arangorestore` output, exit code, and sizes**
   (`pipeline.py:520-534`, `745-764`), and the tuple and graph record
   counts the subprocesses know (`records_in`, `records_out`,
   `records_rejected`).
10. **Batch and CloudWatch correlation:** log AWS_BATCH_JOB_ID, the log
    group and stream, and the flow-run id as correlation_id once
    (`_common.py:391-402`, `release.py:366`). There is no Batch submit call
    in this code, so none exists to instrument.
