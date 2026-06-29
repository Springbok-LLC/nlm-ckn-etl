#!/usr/bin/env python3
# ==============================================================================
# gen-grafana-etl-dashboard.py - Emit the `nlm-ckn-etl` Grafana dashboard
#   (CloudWatch Logs Insights) for the nlm-ckn release/ETL pipeline.
#
#   python3 scripts/gen-grafana-etl-dashboard.py > grafana-etl-dashboard.json
#
# Then import the JSON in Grafana (Dashboards -> New -> Import) and pick your
# CloudWatch datasource.
#
# It reads the single fixed log group /batch/nlm-ckn-release. Each Prefect run
# writes one log stream (release/default/<task-id>), so the dashboard groups
# everything by @logStream -- the newest run sorts to the top of each table.
#
# Panels:
#   - Runs decoder           : release version (RUN_NAME) + tag -> log stream
#   - Flow & subflow results : Completed/Failed lines, newest first
#   - Prefect tasks & status : per run, each task's terminal state (3 flows)
#   - Failures & errors       : ERROR / Failed / Traceback lines
#   - Graph build metrics     : vertices / edges / triples (built + inserted)
#   - Tuples written          : summed `Wrote N tuples` counts per run
#
# Identifying a run by release version: the version (RUN_NAME, e.g. v1.4.7-rc.1)
# is logged once per run in the `Release: tag=.. run=..` line and nowhere else,
# so it can't be attached to every task/metric row in a single Logs Insights
# query (and max()/latest() on a string return null). The decoder table maps
# version <-> @logStream; the metric tables sort newest-first so they line up
# run-for-run with it. To focus one run, paste its @logStream into the `run`
# variable. Blank = every run in the dashboard time range.
#
# Log-line formats this dashboard parses (from real runs):
#   === Starting release: tag=v1.0.0-rc.6 ===
#   Flow run 'eccentric-ape' - Release: tag=v1.0.0-rc.6  run=v1.4.7-rc.1
#   Task run 'extract-release-tarball-85d' - Finished in state Completed()
#   Flow run 'vehement-earwig' - Finished in state Failed("...")
#   Constructed 509516 edges from 2822445 triples in 7.33 s
#   Inserted 281348 vertices in 371.0 s
#   Wrote 2802 tuples to /app/data/tuples-.../...-nsforest.json
# ==============================================================================
import json

LOG_GROUP = "/batch/nlm-ckn-release"
DS = {"type": "cloudwatch", "uid": "${datasource}"}

# `$run` is a @logStream substring filter; blank matches every stream in range.
RUN_FILTER = '| filter @logStream like "$run"\n'


def logs(refId, expr):
    return {
        "refId": refId,
        "datasource": DS,
        "queryMode": "Logs",
        "region": "${region}",
        "expression": expr,
        "logGroupNames": [LOG_GROUP],
        "statsGroups": [],
        "id": "",
    }


_pid = [0]


def panel_id():
    _pid[0] += 1
    return _pid[0]


def logs_panel(title, x, y, w, h, target, desc=""):
    return {
        "id": panel_id(),
        "type": "logs",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target],
        "options": {"showTime": True, "wrapLogMessage": True,
                    "sortOrder": "Descending", "enableLogDetails": True},
    }


def table_panel(title, x, y, w, h, target, desc=""):
    # Logs Insights `stats ... by ...` queries render as a table.
    return {
        "id": panel_id(),
        "type": "table",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target],
        "options": {"showHeader": True},
        "fieldConfig": {"defaults": {"custom": {"align": "auto",
                        "filterable": True}}, "overrides": []},
    }


def row(title, y):
    return {"id": panel_id(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


# Prefect task -> owning flow, taken from the source: release.py defines the
# nlm-ckn-release tasks; pipeline.py defines the nlm-ckn-etl tasks; the fetch
# subflow (nlm-ckn-fetch) runs the external-API tasks. A handful of util tasks
# (sync-results-from-s3, sync-external-from-s3, validate-release-dir,
# validate-external-files) physically run in BOTH fetch and etl; Logs Insights
# can't attribute a task line to a flow, so each is assigned to one panel here
# (fetch, where it first runs) to avoid listing it twice. Its `runs` count still
# reflects every invocation in the stream.
FLOW_TASKS = {
    "release": [
        "extract-release-tarball", "sync-release-dir-to-s3",
        "resolve-fetch-force", "promote-results-to-latest",
    ],
    "fetch": [
        "sync-results-from-s3", "validate-release-dir", "sync-external-from-s3",
        "clean-empty-external-files", "retry-failed-cache-entries",
        "fetch-external-api-results", "transform-external-api-results",
        "validate-external-files", "record-fetch-artifact",
        "sync-external-to-s3-staging", "promote-external-staging",
    ],
    "etl": [
        "ensure-jar", "stop-arangodb", "start-arangodb", "require-arangodb",
        "download-ontologies", "slim-ontologies", "build-ontology-graph",
        "dump-arangodb", "export-graphs-and-analyzers",
        "import-graphs-from-sidecar", "restore-arangodb",
        "sync-baseline-dump-to-s3", "sync-baseline-dump-from-s3",
        "sync-results-dump-to-s3", "sync-results-dump-from-s3",
        "write-tuples", "sync-tuples-to-s3", "validate-tuple-files",
        "build-results-graph", "build-induced-subgraph",
        "create-analyzers-and-views", "promote-to-production",
    ],
}


def flow_tasks_panel(title, x, y, w, h, tasks, desc=""):
    """Per-flow table of each Prefect task's terminal state, newest finish first.

    Task run names carry a random 3-char suffix per instance (e.g.
    write-tuples-bff); strip it to recover the canonical @task(name=..). Group by
    run + task so retries collapse to one row (state = latest, runs = count)."""
    task_list = "[" + ", ".join('"%s"' % t for t in tasks) + "]"
    return table_panel(title, x, y, w, h,
        logs("A",
             "fields @timestamp, @logStream, @message\n"
             + RUN_FILTER +
             "| parse @message /Task run '(?<taskfull>[^']+)' - Finished in state (?<state>\\w+)/\n"
             "| filter ispresent(taskfull)\n"
             "| parse taskfull /(?<taskbase>.+)-[0-9a-z]{3}$/\n"
             "| fields coalesce(taskbase, taskfull) as task\n"
             "| filter task in " + task_list + "\n"
             "| stats latest(state) as last_state, count(*) as runs, "
             "latest(@timestamp) as finished_at by @logStream, task\n"
             "| sort finished_at desc\n"
             "| limit 500"),
        desc=desc)


panels = []

# -- Row: run overview --------------------------------------------------------
panels.append(row("Run overview", 0))

# Decoder: release version <-> log stream, newest first. Each run logs exactly
# one `Release: tag=<src> run=<version>` line (RUN_NAME / NLM_CKN_TAG from the
# Batch job env), so we filter to that line and put the parsed strings in the
# `by` clause -- a string can't be carried through stats any other way
# (max()/latest() on a string field return null in CloudWatch Logs Insights).
# Copy a @logStream from here into the `run` variable to pin every panel to one
# run. Other panels group by @logStream because the version line is the only one
# in the stream that carries the version, so it can't be attached per task row.
panels.append(table_panel(
    "Runs: release version -> log stream (newest first)", 0, 1, 24, 8,
    logs("A",
         "fields @timestamp, @logStream, @message\n"
         + RUN_FILTER +
         "| filter @message like /Release: tag=/\n"
         "| parse @message /Release: tag=(?<source_tag>\\S+)\\s+run=(?<run_version>\\S+)/\n"
         "| stats min(@timestamp) as started by run_version, source_tag, @logStream\n"
         "| sort started desc\n"
         "| limit 50"),
    desc="Decoder mapping each run's release version (RUN_NAME) and source tag "
         "(NLM_CKN_TAG) to its log stream. Top row = latest run. Paste a "
         "@logStream into the `run` dashboard variable to pin all panels to it."))

# -- Row: result & status -----------------------------------------------------
panels.append(row("Result & task status", 9))

# Flow/subflow terminal states, newest first -- eyeball Completed vs Failed.
# The top-level flow is the line ending `for flow 'nlm-ckn-release'`; subflows
# (nlm-ckn-fetch, nlm-ckn-etl) show too so you see exactly where a run died.
panels.append(logs_panel(
    "Flow & subflow results", 0, 10, 24, 5,
    logs("A",
         "fields @timestamp, @logStream, @message\n"
         + RUN_FILTER +
         "| filter @message like /Flow run/ and @message like /Finished in state/\n"
         "| sort @timestamp desc\n"
         "| limit 100"),
    desc="`Flow run '..' - Finished in state ..` lines. Look for Completed vs "
         "Failed; the top-level flow is the nlm-ckn-release one."))

# Each Prefect task's terminal state, split into the three sequential flows so
# the latest run reads as three phases: release -> fetch -> etl. Within each
# table, rows are ordered by finish time (newest first), so since runs are
# time-separated the latest run's tasks sit at the top of each table.
panels.append(flow_tasks_panel(
    "1. Release tasks (nlm-ckn-release)", 0, 15, 8, 12, FLOW_TASKS["release"],
    desc="Top-level release flow: pull the release tarball, push it to S3, "
         "decide fetch-force, and (at the end) promote results to latest."))
panels.append(flow_tasks_panel(
    "2. Fetch tasks (nlm-ckn-fetch)", 8, 15, 8, 12, FLOW_TASKS["fetch"],
    desc="External-API fetch subflow. The shared sync/validate util tasks are "
         "listed here (they also run in etl); `runs` counts every invocation."))
panels.append(flow_tasks_panel(
    "3. ETL tasks (nlm-ckn-etl)", 16, 15, 8, 12, FLOW_TASKS["etl"],
    desc="ETL subflow: build ontology/results/induced graphs, write tuples, "
         "create analyzers/views, and promote to production."))

# -- Row: failures ------------------------------------------------------------
panels.append(row("Failures & errors", 27))

panels.append(logs_panel(
    "Failures & errors", 0, 28, 24, 9,
    logs("A",
         "fields @timestamp, @logStream, @message\n"
         + RUN_FILTER +
         "| filter @message like /Finished in state Failed/ "
         "or @message like /ERROR/ or @message like /Traceback/ "
         "or @message like /CalledProcessError/\n"
         "| sort @timestamp desc\n"
         "| limit 200"),
    desc="ERROR-level logs, failed states, and tracebacks. Empty here = the "
         "runs in range had no errors."))

# -- Row: graph metrics -------------------------------------------------------
panels.append(row("Graph build metrics", 37))

# Vertices / edges / triples, both "Constructed N" and "Inserted N" counts.
# Each appears once per run during the load stage, so max() picks the value.
# Sorted by start time (newest first) so rows line up run-for-run with the
# decoder table above -- match by @logStream there to read the release version.
panels.append(table_panel(
    "Vertices / edges / triples per run", 0, 38, 16, 9,
    logs("A",
         "fields @logStream, @message\n"
         + RUN_FILTER +
         "| parse @message /Constructed (?<edges>\\d+) edges from (?<triples>\\d+) triples/\n"
         "| parse @message /Inserted (?<edges_ins>\\d+) edges/\n"
         "| parse @message /Constructed (?<vertices>\\d+) vertices/\n"
         "| parse @message /Inserted (?<vertices_ins>\\d+) vertices/\n"
         "| filter ispresent(edges) or ispresent(edges_ins) "
         "or ispresent(vertices) or ispresent(vertices_ins)\n"
         "| stats min(@timestamp) as started, max(triples) as triples_total, "
         "max(vertices) as vertices_built, max(vertices_ins) as vertices_inserted, "
         "max(edges) as edges_built, max(edges_ins) as edges_inserted "
         "by @logStream\n"
         "| sort started desc\n"
         "| limit 50"),
    desc="Graph load metrics parsed from `Constructed/Inserted N edges|vertices` "
         "and `N triples` lines, one row per run, newest first. Cross-reference "
         "@logStream with the decoder table for the release version."))

# Tuples are written one file at a time (`Wrote N tuples to ...`); sum per run.
panels.append(table_panel(
    "Tuples written per run", 16, 38, 8, 9,
    logs("A",
         "fields @logStream, @message\n"
         + RUN_FILTER +
         "| parse @message /Wrote (?<n>\\d+) tuples/\n"
         "| filter ispresent(n)\n"
         "| stats min(@timestamp) as started, sum(n) as total_tuples, "
         "count(n) as tuple_files by @logStream\n"
         "| sort started desc\n"
         "| limit 50"),
    desc="Sum of `Wrote N tuples to ...` across all NSForest files in each run, "
         "newest first. Cross-reference @logStream with the decoder table."))

templating = {"list": [
    {
        "name": "datasource", "type": "datasource", "label": "CloudWatch datasource",
        "query": "cloudwatch", "current": {}, "hide": 0, "refresh": 1,
    },
    {
        "name": "region", "type": "constant", "label": "Region",
        "query": "us-east-1", "current": {"text": "us-east-1", "value": "us-east-1"}, "hide": 2,
    },
    {
        # @logStream substring to pin a single run. Look up the stream for a
        # release version in the decoder table at the top, then paste it (or its
        # unique id) here. Blank matches every stream in the dashboard time range.
        "name": "run", "type": "textbox", "label": "Run (paste @logStream from decoder)",
        "query": "", "current": {"text": "", "value": ""}, "hide": 0,
    },
]}

# Grafana's org/shared "Import" validation rejects a raw dashboard model with
# "Old dashboard JSON format ..." unless it carries export-for-sharing metadata.
# __inputs is empty (the CloudWatch datasource is chosen via the `datasource`
# template variable); __requires lists Grafana + the datasource + panel plugins.
_PANEL_PLUGINS = {"table": "Table", "logs": "Logs", "timeseries": "Time series",
                  "stat": "Stat", "barchart": "Bar chart"}


def sharing_requires(panels):
    req = [
        {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "9.0.0"},
        {"type": "datasource", "id": "cloudwatch", "name": "CloudWatch", "version": "1.0.0"},
    ]
    for t in sorted({p["type"] for p in panels} & set(_PANEL_PLUGINS)):
        req.append({"type": "panel", "id": t, "name": _PANEL_PLUGINS[t], "version": ""})
    return req


dashboard = {
    "__inputs": [],
    "__requires": sharing_requires(panels),
    "title": "nlm-ckn-etl",
    "uid": "nlm-ckn-etl",
    "tags": ["nlm-ckn", "etl", "prefect", "cloudwatch"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "1m",
    # Runs take hours; default to a window wide enough to catch the latest one.
    "time": {"from": "now-24h", "to": "now"},
    "templating": templating,
    "panels": panels,
}

print(json.dumps(dashboard, indent=2))
