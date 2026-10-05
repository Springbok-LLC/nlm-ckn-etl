# Logging audit: existing print and log statements

Audit of every console output statement in this repo, done 2026-09-28 to inform
the migration to the structured-logging approach in
`nlm-ckn-rnd/docs/proposals/logging-approach.md`. Line numbers are as of `main`
at `2d28ee3` and will drift.

The audit had two goals: remove messages that are uninformative, and find
places where a message should exist but does not.

| Report | Scope | Sites |
| --- | --- | --- |
| [java.md](java.md) | `src/` (main and test) | 118 |
| [python-scripts.md](python-scripts.md) | `python/src/*.py` (not `flows/`, not `_deprecated/`) and `python/tests` | 191 |
| [flows.md](flows.md) | `python/src/flows/*.py` | 160 |

Each report gives a per-file verdict table (KEEP, MERGE, DEMOTE, DROP, RELEVEL)
with the proposed level, fixed message and fields, then a GAPS section and the
top 10 gaps. The reports were produced by read-only audit agents and have not
been re-verified line by line.

## Totals

Each site gets one verdict: Keep, Merge, Demote or Drop. A fifth verdict,
Re-level, appears only in the flows report, where a site that is kept but
logged at a different level is counted on its own. The Java and Python reports
instead count those sites under Keep and note the level change separately, so
their Re-level figures overlap the other columns and are not part of the
totals.

| Surface | Sites | Keep | Merge | Demote | Drop | Re-level (own verdict) | Level changes (overlap) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Java | 118 | 48 | 37 | 15 | 18 | 0 | about 40 become WARN or ERROR |
| Python scripts | 191 | 71 | 53 | 12 | 55 | 0 | about 20 `WARNING:`/`ERROR:` prefixes become real levels |
| Prefect flows | 160 | 64 | 40 | 7 | 36 | 13 | 13 |
| **Total** | **469** | **183** | **130** | **34** | **109** | **13** | |

The columns sum to the site count: 183 + 130 + 34 + 109 + 13 = 469.

A mechanical conversion would carry over 469 lines. After the audit, about 240
lines remain at INFO or above: 183 kept and 13 re-leveled sites, plus the 130
merged sites collapsing into about 40 to 50 summary lines. The 34 demoted
sites remain as DEBUG lines. The 109 drops are mostly `"="*70` banners,
`Getting X` and `Creating X` per-call chatter, "Running X" lines that
duplicate the flow, and per-item "could not find" lines that become one
`records_rejected` count.

## Patterns that repeat across the audits

1. **Per-item lines become one count.** Thousands of "could not map gene X" and
   "no data for Y" prints become a single `records_rejected` line with a
   `reason` field (`LoaderUtilities`, the tuple writers, `DataTransformer`).
2. **Real levels replace text prefixes.** `WARNING:` and `ERROR:` strings,
   `System.err`, `warnings.warn` and `printStackTrace` become WARN or ERROR.
3. **Subprocess launches are the natural `phase` boundaries.** Every flow-side
   Python and Java launch needs a start line and an exit line (rc,
   `duration_ms`). The subprocess helpers log nothing today.
4. **Every `main()` needs start, finish and failure lines.** That is the 10 Java
   classes and the Python scripts, with `duration_ms` and an outcome. Java
   `main()`s currently rethrow with no log line.

## Correctness bugs found

These affect the data, not only the logging. The first six were fixed ahead of
the logging work:

| Bug | PR |
| --- | --- |
| Fetch failures cached as successes (`DataFetcher.py:216-234`, `188`, `855-856`) | #103 |
| Tuple writers exit 0 on missing input | #104 |
| Sidecar export and import failures only warn (`pipeline.py`) | #105 |
| No-stage-flags returns success; `OntologyElementParser` exits 0 on no files | #106 |
| `OntologyDownloader` deletes a good download when a version is null, and a failed URL aborts the loop | #107 |
| `OntologyGraphBuilder` counts attempts as inserts and swallows insert failures | #108 |

Not fixed, deliberately left to the logging work:

- `ResultsGraphBuilder:362` prints and calls `System.exit(1)`. It already fails;
  the gap is an ERROR log line.
- `ResultsGraphBuilder:285` overwrites the edge label on collision where
  `OntologyGraphBuilder:472` appends. May be intentional.
- `OntologySlimmer:517, 539` archives its input even when 0 classes were kept.
- Silent record drops with no `records_rejected` count (see the Java and Python
  reports, GAPS).

## Decisions taken

- Behavior-bug fixes first, as separate small PRs to `main`.
- Logging work goes through the long-lived `logging-migration` branch.
- Skip `PhenotypeGraphBuilder` (`@Deprecated`).
- Strict fixed `message` plus fields everywhere; `source_version` as a field on
  fetch lines; a `LOG_FORMAT=console` escape hatch for local runs.

## Out of repo

- The Batch job definition sets the container environment, so `CORRELATION_ID`
  cannot be passed at submit from this repo. There is no Batch submit call here.
- CloudWatch log groups, metric filters and Grafana.

## Planned phases

0. Foundation: `logging_setup.py`, `structlog`, and `CORRELATION_ID` and `PHASE`
   read from the environment so the worker scripts (separate processes) join the
   run.
0.5. Message conventions: the `snake_case` message vocabulary and `reason`
   values, so the three surfaces do not invent their own. Done, in
   [../logging-conventions.md](../logging-conventions.md).
1. Correlation plumbing: a `_log_env()` helper merged into every subprocess
   environment.
2. Flows: structlog, a subprocess wrapper with start and exit lines, a
   structured cache decision in place of the `log=` callback, and flow-start
   and flow-end summary lines.
3. Python scripts: `print` to structured logging, with per-source
   `fetch_finished` lines and Open Targets accumulation.
4. Java: a shared `LogContext.init()` and run wrapper, `logstash-logback-encoder`,
   `records_rejected` counters.
5. Counts: fetch, transform and load boundaries per the proposal's section 4.
6. Tests and docs, including the stale `flows/README.md`.
