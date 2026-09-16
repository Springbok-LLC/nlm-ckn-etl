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

To protect concurrent pipeline runs, new data is written to
`runs/{run}/external-staging/` first and only promoted to the live `external/`
prefix after the full fetch passes validation.

**Key parameters**

| Option | Default | Description |
|--------|---------|-------------|
| `--force` | off | Wipe the local cache and re-fetch all sources from scratch |
| `--retry-empty` | off | Retry only previously-failed entries; keep all successful data |
| `--max-fetch-age-hours` | `0` (off) | When > 0 and `--force` is not set, auto-force a full re-fetch if the cache is missing, older than this, or produced by different fetch code; otherwise reuse and retry empties. Used by the scheduled fetch in place of `--force`. |
| `--run-name` | `$CKN_RUN` / `full` | Selects `data/external-{run}/` locally and `runs/{run}/01-results/` in S3 |

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

Drives a full release from an nlm-ckn GitHub tag. Runs the three steps in
sequence and promotes `runs/latest/` at the end only on full success.

**Step 1** — Download and extract the release tarball into `data/results-{run}/`
and push to `runs/{run}/01-results/` in S3.

**Step 2** — Refresh the external API cache via `fetch.py`. Checks the age
of `external/fetch-info.json` in S3 against `--max-fetch-age-hours` (default
672 h = four weeks). If the cache is fresh enough, uses `retry_empty=True` so
only new or previously-failed entries are re-fetched — the bulk of the data is
served from the cache built by the scheduled fetch. If the cache is missing,
older than the threshold, or was produced by different fetch code, forces a
full re-fetch.

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
                        writes → runs/{run}/external-staging/   (new data, after validation)
                        copies → runs/{run}/external-staging/ → external/  (atomic promotion)

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
                        writes → runs/{run}/external-staging/ → external/

             Step 3:    pipeline.py (inline)
                        (see S3 layout below for per-phase reads/writes)

             On success:
                        copies → runs/{run}/01-results/ → runs/latest/01-results/
```

The key design consequence: a new release is fast to iterate when the
scheduled fetch has been running. If there are issues with a new dataset,
re-running Step 2 and Step 3 alone uses the already-warm cache rather than
re-fetching hours of API data (Phase 1 is skipped automatically when its
baseline dump already exists).

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
│   └── fetch-info.json              # Timestamp + file sizes for the last fetch
│
├── baselines/
│   └── {jar_key}/                   # 16-char JAR content hash
│       └── baseline-dump.tar.gz     # ArangoDB dump after Phase 1 (ontology)
│
└── runs/
    ├── latest/
    │   └── 01-results/              # Stable pointer → last successful release
    │       └── results_ensg_*.csv
    │
    └── {run}/                       # One directory per release tag (e.g. 0.0.0-alpha)
        ├── 01-results/              # Flat NSForest result CSVs + hubmap_urls.txt
        ├── 02-external.tar.gz       # Snapshot of external/ used for this run
        ├── 03-obo.tar.gz            # OWL ontology files
        ├── 05-tuples.tar.gz         # Tuple JSON files
        ├── 06-golden-dump.tar.gz    # Final ArangoDB dump (production artifact)
        ├── 07-kgx.tar.gz            # KGX TSV node/edge pairs of both databases
        ├── build-info.txt           # Version metadata (date, commit, fetch info)
        ├── {tarball}                # Release tarball staged by trigger-release.sh
        ├── release.json             # Release config staged by trigger-release.sh
        └── external-staging/        # In-flight fetch output (mirrors external/)
            └── ...                  # Promoted to external/ after validation
```

### Read/write by flow and phase

| S3 path | Written by | Read by |
|---------|------------|---------|
| `external/` | `fetch.py` (via staging promotion) | `fetch.py` (resume), `pipeline.py` Phase 2 |
| `external/fetch-info.json` | `fetch.py` | `release.py` (`resolve_fetch_force`) |
| `baselines/{jar_key}/baseline-dump.tar.gz` | `pipeline.py` Phase 1 | `pipeline.py` Phase 2 |
| `runs/latest/01-results/` | `release.py` (on full success) | `fetch.py` (scheduled task) |
| `runs/{run}/release.json` | `trigger-release.sh` | `release.py` (via `RELEASE_CONFIG` env var) |
| `runs/{run}/{tarball}` | `trigger-release.sh` | `release.py` Step 1 |
| `runs/{run}/01-results/` | `release.py` Step 1 | `pipeline.py` Phase 2, `fetch.py` (non-scheduled) |
| `runs/{run}/external-staging/` | `fetch.py` | `fetch.py` (promotion source) |
| `runs/{run}/02-external.tar.gz` | `pipeline.py` Phase 3 | audit / manual restore |
| `runs/{run}/03-obo.tar.gz` | `pipeline.py` Phase 3 | audit / manual restore |
| `runs/{run}/05-tuples.tar.gz` | `pipeline.py` Phase 2 | audit / manual restore |
| `runs/{run}/06-golden-dump.tar.gz` | `pipeline.py` Phase 3 | production ArangoDB restore, `pipeline.py` `--force-kgx` |
| `runs/{run}/07-kgx.tar.gz` | `pipeline.py` Phase 3 (or `--force-kgx`) | `nlm-ckn-ui` / manual Neo4j+Jena+ArangoDB load |
| `runs/{run}/build-info.txt` | `pipeline.py` Phase 3 | audit |

---

## Local Usage

Run from the `python/` directory with ArangoDB running (`docker compose up -d`
from the repo root). `S3_BUCKET` is optional — omit it to work entirely on
local disk.

```bash
# Full release (reuses a cache younger than four weeks by default;
# also the command to re-run after a failed Step 3 — cache and results
# already in S3 are reused)
poetry run src/flows/release.py \
  --nlm-ckn-tag v0.0.2 \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Override the cache-age threshold (force a re-fetch unless the cache is < 48 h old)
poetry run src/flows/release.py \
  --nlm-ckn-tag v0.0.2 \
  --max-fetch-age-hours 48 \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Fetch only (refresh external cache without a full release)
poetry run src/flows/fetch.py \
  --ncbi-email user@example.com \
  --ncbi-api-key KEY

# Pipeline only (external cache and results already present locally or in S3)
poetry run src/flows/pipeline.py --run-results --run-name 2026-04
poetry run src/flows/pipeline.py --run-ontology --run-results --run-archive --run-name 2026-04
```
