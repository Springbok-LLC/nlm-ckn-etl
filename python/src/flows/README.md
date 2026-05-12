# NLM-CKN ETL Flows

Three Prefect flows orchestrate the pipeline. Each can run standalone or as
part of a full release.

---

## Flows

### `fetch.py` — External API Fetch

Downloads raw data from CELLxGENE, Open Targets, NCBI Gene, UniProt, and
HuBMAP into a local cache and pushes it to S3. Designed to run independently
on a recurring schedule (ECS Fargate + EventBridge Scheduler) so the cache
stays warm between releases.

Reads the NSForest results directory to determine which genes to fetch. The
scheduled task always reads from `runs/latest/01-results/` (see
[Lifecycle](#lifecycle) below).

To protect concurrent pipeline runs, new data is written to the
`external-staging/` S3 prefix first and only promoted to the live `external/`
prefix after the full fetch passes validation.

**Key parameters**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `force` | `False` | Wipe the local cache and re-fetch all sources from scratch |
| `retry_empty` | `False` | Retry only previously-failed entries; keep all successful data |
| `run` | `$CKN_RUN` / `full` | Selects `data/external-{run}/` locally and `runs/{run}/01-results/` in S3 |

---

### `pipeline.py` — Three-Phase ETL

Processes cached external data into a production ArangoDB database.

**Phase 1 — Ontology build** (`--run-ontology`): Downloads OWL ontologies,
slims them, loads into ArangoDB, and saves a baseline dump. Expensive (hours)
but only needs to re-run when ontologies change.

**Phase 2 — Results** (`--run-results`): Restores the baseline dump, writes
all tuples, and builds the results, induced-subgraph, and phenotype graphs.
Fully repeatable without re-running Phase 1.

**Phase 3 — Archive** (`--run-archive`): Dumps the final database state as
the golden artifact and promotes all production artifacts to
`s3://bucket/runs/{run}/`.

---

### `release.py` — End-to-End Release

Drives a full release from a cell-kn GitHub tag. Runs the three steps in
sequence and promotes `runs/latest/` at the end only on full success.

**Step 1** — Download and extract the release tarball into `data/results-{run}/`
and push to `runs/{run}/01-results/` in S3.

**Step 2** — Refresh the external API cache via `fetch.py`. Checks the age
of `external/fetch-info.json` in S3 against `--max-fetch-age-hours` (default
48 h). If the cache is fresh enough, uses `retry_empty=True` so only new or
previously-failed entries are re-fetched — the bulk of the data is served
from the cache built by the scheduled fetch. If the cache is stale or absent,
forces a full re-fetch.

**Step 3** — Runs the full three-phase ETL pipeline.

**On success** — Promotes `runs/{run}/01-results/` to `runs/latest/01-results/`
so the next scheduled fetch targets the new release's gene set. If any step
fails, `latest` is never updated.

---

## Lifecycle

```
             ┌─────────────────────────────────────────────┐
             │  EventBridge Scheduler (daily, 02:00 UTC)   │
             └────────────────┬────────────────────────────┘
                              │
                              ▼
                        fetch.py (ECS Fargate)
                        reads  → runs/latest/01-results/  (last good release gene set)
                        reads  → external/                (resume from last complete cache)
                        writes → external-staging/        (new data, after validation)
                        copies → external-staging/ → external/  (atomic promotion)

             ┌─────────────────────────────────────────────┐
             │  release.py (manual / CI trigger)           │
             └────────────────┬────────────────────────────┘
                              │
             Step 1:          ▼
                        Extract tarball → data/results-{run}/
                        writes → runs/{run}/01-results/

             Step 2:    fetch.py (inline)
                        reads  → external/fetch-info.json  (check cache age)
                        if fresh: retry_empty=True          (fast — bulk served from cache)
                        if stale: force=True                (full re-fetch)
                        writes → external-staging/ → external/

             Step 3:    pipeline.py (inline)
                        (see S3 layout below for per-phase reads/writes)

             On success:
                        copies → runs/{run}/01-results/ → runs/latest/01-results/
```

The key design consequence: a new release is fast to iterate when the
scheduled fetch has been running. If there are issues with a new dataset,
re-running Step 2 and Step 3 alone (`--skip-ontology`) uses the already-warm
cache rather than re-fetching hours of API data.

---

## S3 Bucket Layout

```
s3://{S3_BUCKET}/
│
├── external/                        # Live external API cache
│   ├── cellxgene.json
│   ├── cellxgene_transformed.json
│   ├── opentargets.json
│   ├── opentargets_transformed.json
│   ├── gene.json
│   ├── gene_transformed.json
│   ├── uniprot.json
│   ├── uniprot_transformed.json
│   ├── pubmed.json
│   └── fetch-info.json              # Timestamp + file sizes for the last fetch
│
├── external-staging/                # In-flight fetch output (mirrors external/)
│   └── ...                          # Promoted to external/ after validation
│
├── runs/
│   ├── latest/
│   │   └── 01-results/              # Stable pointer → last successful release
│   │       └── *_results.csv
│   │
│   └── {run}/                       # One directory per release (e.g. 2026-04)
│       ├── 01-results/              # Flat NSForest result CSVs + hubmap_urls.txt
│       ├── 02-external.tar.gz       # Snapshot of external/ used for this run
│       ├── 03-obo.tar.gz            # OWL ontology files
│       ├── 04-baseline-dump.tar.gz  # ArangoDB baseline dump (post-ontology)
│       ├── 05-tuples.tar.gz         # Tuple JSON files
│       ├── 06-golden-dump.tar.gz    # Final ArangoDB dump (production artifact)
│       └── build-info.txt           # Version metadata (date, commit, fetch info)
│
└── artifacts/
    └── nlm-ckn-etl-1.0.jar         # Compiled Java JAR (written by CI/CD)
```

### Read/write by flow and phase

| S3 path | Written by | Read by |
|---------|------------|---------|
| `external/` | `fetch.py` (via staging promotion) | `fetch.py` (resume), `pipeline.py` Phase 2 |
| `external-staging/` | `fetch.py` | `fetch.py` (promotion source) |
| `external/fetch-info.json` | `fetch.py` | `release.py` (`resolve_fetch_force`) |
| `runs/latest/01-results/` | `release.py` (on full success) | `fetch.py` (scheduled task) |
| `runs/{run}/01-results/` | `release.py` Step 1 | `pipeline.py` Phase 2, `fetch.py` (non-scheduled) |
| `runs/{run}/02-external.tar.gz` | `pipeline.py` Phase 3 | audit / manual restore |
| `runs/{run}/03-obo.tar.gz` | `pipeline.py` Phase 3 | audit / manual restore |
| `runs/{run}/04-baseline-dump.tar.gz` | `pipeline.py` Phase 1 | `pipeline.py` Phase 2 |
| `runs/{run}/05-tuples.tar.gz` | `pipeline.py` Phase 2 | audit / manual restore |
| `runs/{run}/06-golden-dump.tar.gz` | `pipeline.py` Phase 3 | production ArangoDB restore |
| `runs/{run}/build-info.txt` | `pipeline.py` Phase 3 | audit |
| `artifacts/nlm-ckn-etl-1.0.jar` | CI/CD (`build-jar.yml`) | `pipeline.py` (`ensure_jar`) |

---

## Local Usage

Run from the `python/` directory with ArangoDB running (`docker compose up -d`
from the repo root). `S3_BUCKET` is optional — omit it to work entirely on
local disk.

```bash
# Full release
poetry run src/flows/release.py \
  --tag v0.0.2 \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Release reusing a fresh cache (skip re-fetch when cache < 48 h old)
poetry run src/flows/release.py \
  --tag v0.0.2 \
  --max-fetch-age-hours 48 \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Re-run ETL only after a failed Step 3 (cache and results already in S3)
poetry run src/flows/release.py \
  --tag v0.0.2 \
  --skip-ontology \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Fetch only (refresh external cache without a full release)
poetry run src/flows/fetch.py \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Pipeline only (external cache and results already present locally or in S3)
poetry run src/flows/pipeline.py --run-results --run 2026-04
poetry run src/flows/pipeline.py --run-ontology --run-results --run-archive --run 2026-04
```
