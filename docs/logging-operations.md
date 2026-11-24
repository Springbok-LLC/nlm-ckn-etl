# Reading the logs

How to read what the ETL logs, for whoever runs a release or looks into one
afterwards. The names and fields are defined in
[logging-conventions.md](logging-conventions.md); the schema and CloudWatch
wiring are in `nlm-ckn-rnd/docs/proposals/logging-approach.md`.

## Format

Every process (the Prefect flows, the Python workers, the Java programs) writes
one JSON object per line to stdout. `LOG_FORMAT` chooses the rendering:

| `LOG_FORMAT` | Output |
| --- | --- |
| unset, or anything but `console` | JSON, for CloudWatch and for `jq` |
| `console` | readable lines, for a terminal |

`LOG_FORMAT=console` is for watching a local run. It is not a way to read a
production log, and the flow cannot total what its children logged under it
(see the run summary below). `ProductionDataValidator` defaults to `console`,
since it is run by hand; set `LOG_FORMAT=json` to get JSON from it, on stderr.

Each JSON line carries `timestamp` (UTC, ISO 8601), `level` (`DEBUG`, `INFO`,
`WARN`, `ERROR`), `service`, `correlation_id`, `logger`, `message` and
`release`, plus `phase` where one applies and the fields of the event. `message`
is a fixed event name, so filter on it rather than searching text.

```
jq -c 'select(.level == "ERROR")' run.log
```

## One run, one id

`correlation_id` is the join key for a run. It is `CORRELATION_ID` from the
environment when set (a caller or the Batch job definition can fix one), else
the id the flow is already logging under, else the Prefect flow run id. The
flows pass it to every subprocess, so Python and Java lines from one run share
it. `GIT_SHA` becomes `release`.

## The run summary

Read these before the rest of the log.

| Event | What it tells you |
| --- | --- |
| `run_finished` | `outcome`, `duration_ms`, and for the pipeline `phases_run`, `phases_skipped`, `phase_durations_ms` and `phases`, which holds what each phase's `phase_finished` reported |
| `run_failed` | `error_type`, `error`, `phase` in progress and `task` |
| `phase_finished` | `duration_ms` and counts. In the pipeline, `loads` totals what the phase's children loaded (keyed by event, such as `vertices_inserted`, each with `events` and the sums of `records_in`, `records_out`, `records_rejected` and the like) and `database_totals` gives each database's `vertex_total` and `edge_total` |
| `fetch_source_status` | One per source at the end of the fetch flow; a stale or failing source shows as a WARN |
| `validation_finished` | `n_errors`, `n_warnings`, `exit_code` from the data validator |

`loads` and `database_totals` are left out of a phase whose children logged
nothing for them, and are absent under `LOG_FORMAT=console`.

## Logs Insights

These use only events and fields listed in the conventions. CloudWatch Logs
Insights discovers JSON fields, and reaches nested ones with dots. They have not
been run against a production log group; adjust the field paths if Insights
spells one differently.

```
# Errors by phase
filter level = "ERROR" | stats count() by phase

# A run, in order
filter correlation_id = "<id>" | sort @timestamp asc

# How each run ended
filter message = "run_finished" | fields @timestamp, correlation_id, outcome, duration_ms

# Why a run failed
filter message = "run_failed" | fields @timestamp, correlation_id, phase, task, error_type, error

# Rejected records by source and phase
filter ispresent(records_rejected) | stats sum(records_rejected) by source, phase

# Slow phases
filter message = "phase_finished" | stats max(duration_ms) by phase

# Sources that are stale or failing
filter message = "fetch_source_status" and level = "WARN" | fields source, outcome, last_success_at, age_hours
```
