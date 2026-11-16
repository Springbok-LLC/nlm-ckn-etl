# `src/main/shell/` — operational scripts

This directory holds the shell scripts that build, deploy, trigger, and locally
operate the NLM-CKN ETL pipelines. They fall into four groups:

1. **Container entrypoints** — baked into the Docker images; run inside AWS.
2. **Release & fetch triggers / infra deploy** — stand up and launch the AWS pipelines.
3. **CI / build glue** — invoked by GitHub workflows (see `.github/workflows/`).
4. **Local developer / ops utilities** — run by hand on a workstation; not used by CI.

Everything turns on the multi-stage root **`Dockerfile`**, which produces two images,
each with one of these scripts as its `ENTRYPOINT`.

---

## The two container images

`Dockerfile` stages: `java-build` (compiles the JAR, line 77) → `base`
(Python 3.12 + Poetry-exported deps; the layer defined by
[`Dockerfile.python`](Dockerfile.python), line 94) → two leaf targets:

| Target (`--target`) | `ENTRYPOINT` | Runs |
|---|---|---|
| `fetcher` (Dockerfile:163) | [`fetch-entrypoint.sh`](fetch-entrypoint.sh) (Dockerfile:165,173) | `python src/flows/fetch.py … --force` |
| `pipeline` (Dockerfile:187) | [`release-entrypoint.sh`](release-entrypoint.sh) (Dockerfile:197,201) | `python src/flows/release.py "${args[@]}"` |

Both entrypoints are thin shims: validate required env vars, assemble args, then
`exec` the Prefect flow. Both images are built and pushed to ECR by
`build-image.yml` (and by the `deploy-*.sh` scripts for initial setup).

---

## 1. Release pipeline → `release.py`

Triggered by a **published GitHub Release** (automatic) or **manual dispatch**.
`release.json` (repo root) is the config contract threaded through every layer.

```
GitHub Release published                  manual: gh workflow run trigger-release.yml
  on-release.yml                             trigger-release.yml  (workflow_dispatch)
    ├─ test  → ci.yml (quality gate)
    ├─ build → build-image.yml → push pipeline+fetcher images to ECR
    └─ submit ──────────────┐                        └──────────────┐
                            ▼                                        ▼
                 trigger-release.sh   (on-release.yml:72 / trigger-release.yml:76)
                   • reads release.json (nlm_ckn_tag, tar_source, max_fetch_age_hours, hubmap_urls)
                   • stages tarball + release.json → S3
                   • creates a GitHub deployment status
                   • registers a new AWS Batch job-definition revision (with the ECR image)
                   • aws batch submit-job
                            ▼
                 AWS Batch container → ENTRYPOINT release-entrypoint.sh
                   • validates NLM_CKN_TAG, S3_BUCKET, NCBI_EMAIL, NCBI_API_KEY, RELEASE_CONFIG
                   • exec python src/flows/release.py --nlm-ckn-tag … --release-config …
```

| File | Role |
|---|---|
| [`trigger-release.sh`](trigger-release.sh) | Stage inputs to S3, register Batch job def, submit the Batch release job. Returns immediately (does not wait). |
| [`release-entrypoint.sh`](release-entrypoint.sh) | Batch container entrypoint; validates env and `exec`s `release.py`. |
| `.github/workflows/on-release.yml` | On `release: published` → test → build image → submit (run name = release tag). |
| `.github/workflows/trigger-release.yml` | `workflow_dispatch` with inputs (`nlm_ckn_tag`, `tar_source`, `run_name`, `max_fetch_age_hours`) → `trigger-release.sh`. |
| `.github/workflows/on-release-deactivate.yml` | On `release: deleted`/`unpublished` → mark/delete the GitHub deployments (GitHub-only, no AWS). |

> **Note:** `release.json` is read by `trigger-release.sh` *and again* by `release.py`.
> Keep flag names consistent across both, plus the entrypoint scripts — this chain
> is a CI/CD contract across ~5 layers.

---

## 2. Fetch pipeline → `fetch.py`

Triggered by a **schedule**, not a workflow.

```
EventBridge Scheduler  cron(0 2 * * ? *)   (daily 02:00 UTC; SCHEDULE_EXPRESSION)
  → ECS Fargate task (fetcher image)
    → ENTRYPOINT fetch-entrypoint.sh
      → python src/flows/fetch.py --ncbi-email … --ncbi-api-key … --force
```

| File | Role |
|---|---|
| [`fetch-entrypoint.sh`](fetch-entrypoint.sh) | Fargate container entrypoint; `exec`s `fetch.py --force`. |

The scheduled fetch always passes `--force`. It writes to `external-staging/` in S3,
then atomically promotes to `external/`, which release runs read.

---

## 3. CI / build / promote workflows (`.github/workflows/`)

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push/PR on `src/**`, `python/**`, `pom.xml`, `Dockerfile`; also `workflow_call` | Quality gate: calls `java-build.yml` + `python-lint.yml`, then a Docker smoke test (`--target pipeline`, no push). |
| `java-build.yml` | `workflow_call` | `mvn test` + test report. |
| `python-lint.yml` | `workflow_call` | `poetry install` → `ruff check` → `pytest`. |
| `build-image.yml` | `workflow_call` (from `on-release.yml`), `workflow_dispatch` | Resolve ECR URIs from the `ecr` CloudFormation stack; build & push `pipeline` and `fetcher` images (tag = branch, plus `latest` on main/master). **Not** run on every commit. |
| `promote-to-upstream.yml` | push to `main` (Springbok-LLC only), `workflow_dispatch` | Fast-forward (or PR + admin-merge) `NIH-NLM` upstream. GitHub-only. |

---

## 4. Infrastructure deploy scripts (run by hand, occasional)

Not referenced by any workflow — these create the AWS resources the pipelines run on
(via `cloudformation/*.yaml`). Require `S3_BUCKET`, `VPC_ID`, `SUBNET_IDS` (and NCBI
creds for fetch); read an optional `.env`.

| Script | Deploys |
|---|---|
| [`deploy-batch.sh`](deploy-batch.sh) | `ecr.yaml` + build/push pipeline image + `batch.yaml` (Batch queue + job def for releases). Optionally creates the GitHub-token secret. |
| [`deploy-fetch.sh`](deploy-fetch.sh) | `ecr.yaml` + build/push fetcher image + `fetch.yaml` (EventBridge schedule + Fargate task). `SCHEDULE_EXPRESSION` default `cron(0 2 * * ? *)`. |

---

## 5. Local developer / ops utilities

None of these are used by CI or the production entrypoints — they are for working
with graphs on a workstation or reaching AWS for diagnostics.

### Local ArangoDB lifecycle
| Script | Purpose |
|---|---|
| [`start-arangodb.sh`](start-arangodb.sh) / [`stop-arangodb.sh`](stop-arangodb.sh) | Start / stop a local ArangoDB Docker container on `:8529`. |

### Dump transfer (S3 ↔ local)
| Script | Purpose |
|---|---|
| [`load-dump-local.sh`](load-dump-local.sh) | Download a `runs/<version>/06-golden-dump.tar.gz` from S3 (`cell-kn-arangodb-data-…`) and `arangorestore` it into the local container, re-importing named graphs + analyzers. **The usual way to inspect a prior release locally.** |
| [`download-arangodb.sh`](download-arangodb.sh) / [`upload-arangodb.sh`](upload-arangodb.sh) | `kgx` TSV round-trip: export the phenotype DB to TSV / re-import it. |
| [`export-arangodb.sh`](export-arangodb.sh) | Export the `Cell-KN-Ontologies` `CL` collection via `arangoexport`. *(Narrow/likely vestigial.)* |

### Remote access & observability (AWS)
| Script | Purpose |
|---|---|
| [`arango-tunnel.sh`](arango-tunnel.sh) | Open an SSM port-forward to a remote ArangoDB EC2 instance by environment (`dev`/`stage`/`sandbox`/`prod`); pulls the password from Secrets Manager. |
| [`download-logs.sh`](download-logs.sh) | Fetch a Batch release job's CloudWatch logs (`/batch/nlm-ckn-release`). Handy for post-release diagnostics. |

### Legacy / alternative stores
`start-jena.sh` / `stop-jena.sh` / `upload-jena.sh` (Fuseki RDF) and
`start-neo4j.sh` / `stop-neo4j.sh` / `upload-neo4j.sh` (Neo4j) drive alternative
graph backends. The ETL targets ArangoDB; these are exploratory and effectively
**legacy**.

---

## Quick reference: which script runs where

| Context | Scripts |
|---|---|
| Inside AWS (container entrypoints) | `fetch-entrypoint.sh`, `release-entrypoint.sh` |
| Invoked by GitHub workflows | `trigger-release.sh` (via `on-release.yml` / `trigger-release.yml`) |
| Run by hand to set up AWS | `deploy-batch.sh`, `deploy-fetch.sh` |
| Run by hand on a workstation | everything in §5 |
