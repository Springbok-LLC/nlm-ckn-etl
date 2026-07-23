# NLM-CKN Extraction, Translation, and Loading

## Motivation

The National Library of Medicine (NLM) Cell Knowledge Network (NLM-CKN)
provides a comprehensive cell phenotype knowledge network that integrates
knowledge about diseases and drugs to facilitate discovery of new biomarkers
and therapeutic targets. To maximize interoperability of the derived knowledge
with knowledge about genes, pathways, diseases, and drugs from other NLM/NCBI
resources, the knowledge will be derived in the form of semantically-structured
assertions of subject-predicate-object triple statements which are compatible
with semantic web technologies, and storage using graph databases, such as the
[ArangoDB](https://arangodb.com/) database system.

The NLM-CKN captures single cell genomics data from existing data repositories,
such as CELLxGENE, and uses NSForest to identify cell type-specific marker
genes. The cell types are manually mapped to the Cell Ontology (CL), and the
marker genes are linked to data from external sources, such as Open Targets, to
provide relationships to diseases and drugs. In addition, the NLM-CKN uses
natural language processing to extract information about cell type-specific
marker genes, and their association with disease state from open access
peer-reviewed publications.

## Purpose

This repository provides:

- **A Java package** for parsing ontology OWL files, loading semantic triples
  into an ArangoDB instance, and identifying relevant subgraphs
- **Python modules** for parsing and loading ontologies, fetching data from
  external sources, and creating semantic triples from NSForest results, manual
  CL mappings, external data, and NLP results

This is a unified repository that combines the previously separate
`nlm-ckn-mvp-etl-ontologies` and `nlm-ckn-mvp-etl-results` repositories,
eliminating the need for git submodules and system-scoped JAR dependencies.

The pipeline produces an ArangoDB archive from single cell genomics results
and source ontologies, as shown below:

![NLM-CKN ETL pipeline: a Data Processing Pipeline (DataFetcher → DataTransformer → TupleWriters → ResultsGraphBuilder) and an Ontology Processing Pipeline (OntologyDownloader → OntologyGraphBuilder) feeding ArangoDB graph storage, from which InducedSubgraphBuilder selects a relevant subgraph](docs/images/NLM-CKN-ETL.png)

## Project Structure

```
nlm-ckn-etl/
├── pom.xml                          # Maven POM (all Java dependencies)
├── Dockerfile                       # Multi-stage build (Java + Python images)
├── compose.yaml                     # Local Docker Compose services
├── release.json                     # Release settings (tag, tar source, etc.)
├── src/
│   ├── main/
│   │   ├── java/gov/nih/nlm/        # Java classes
│   │   └── shell/                   # ArangoDB shell scripts
│   └── test/
│       ├── java/gov/nih/nlm/        # Java test classes
│       └── data/
│           ├── obo/                 # Sample ontology files
│           ├── summaries/           # Sample summary inputs
│           └── tuples/              # Sample tuple inputs
├── python/
│   ├── pyproject.toml               # Poetry configuration
│   ├── src/                         # Python modules
│   └── tests/                       # Python test files
└── docs/
    ├── java/                        # Generated Javadoc output (gitignored)
    └── python/                      # Sphinx documentation
```

## Ontologies

All terms from the following ontologies have been selected for loading into the
NLM-CKN:

- [CL](http://purl.obolibrary.org/obo/cl.owl): Cell Ontology
- [GO](https://purl.obolibrary.org/obo/go/extensions/go-plus.owl): Gene Ontology
- [UBERON](http://purl.obolibrary.org/obo/uberon/uberon-base.owl): Uberon multi-species anatomy ontology
- [NCBITaxon](http://purl.obolibrary.org/obo/ncbitaxon/subsets/taxslim.owl): NCBI organismal taxonomy
- [MONDO](http://purl.obolibrary.org/obo/mondo/mondo-simple.owl): Mondo Disease Ontology
- [HP](http://purl.obolibrary.org/obo/hp.owl): Human Phenotype Ontology
- [PATO](http://purl.obolibrary.org/obo/pato.owl): Phenotype And Trait Ontology
- [HsapDv](http://purl.obolibrary.org/obo/hsapdv.owl): Human Developmental Stages

Selected terms from the following ontology have also been selected for loading:

- [PRO](http://purl.obolibrary.org/obo/pr.owl): PRotein Ontology

## External Sources

Data can be fetched from the following external sources:

- [Open Targets](https://www.opentargets.org/): Includes diseases,
  drugs, interactions, pharmacogenetics, tractability, expression, and
  depmap resources. Released as discrete batches on a roughly **quarterly**
  cycle, versioned `YY.MM` (e.g. 26.03); pin to a specific release. The
  cadence is not perfectly clockwork — quarters are occasionally skipped.
- [Gene](https://www.ncbi.nlm.nih.gov/gene/): Records include
  nomenclature, Reference Sequences (RefSeqs), maps, pathways,
  variations, phenotypes, and links to genome-, phenotype-, and
  locus-specific resources. Continuously updated (nomenclature **daily**,
  most GeneRIFs **weekly**, ~2 days to propagate) with **no discrete version**.
  The NCBI Datasets gene package inherits this rolling behavior, so for a
  reproducible checkpoint pin to the underlying **RefSeq release number**
  (bi-monthly, odd months) or the per-organism **genome annotation
  release / assembly accession**, and capture per-record modification dates
  for live lookups.
- [UniProt](https://www.uniprot.org/): Includes protein sequence, and
  functional information resources. Released on a fixed **8-week** cycle
  (~6–7/year), versioned `YYYY_XX` (e.g. 2026_01); each release is a clean,
  citable snapshot to pin to.

For reproducibility, align re-syncs to the slowest meaningful cadence (the
Open Targets quarterly release) and record the exact version identifier for
each source at every sync. See
[`data-source-update-cadences.md`](docs/data-source-update-cadences.md) for the
full breakdown and sources.

## Dependencies

### Docker

Install [Docker Desktop](https://docs.docker.com/desktop/).

### ArangoDB

An ArangoDB docker image can be downloaded and a container started from the
repository root directory as follows
```
$ export ARANGO_DB_HOST=127.0.0.1
$ export ARANGO_DB_PORT=8529
$ export ARANGO_DB_HOME="<some-path>/arangodb"
$ export ARANGO_DB_APPS=$ARANGO_DB_HOME/arangodb-apps
$ export ARANGO_DB_USER=root
$ export ARANGO_DB_PASSWORD="<some-password>"
$ export ARANGO_ONTOLOGY_DB_NAME=Cell-KN-Ontologies
$ export ARANGO_PHENOTYPE_DB_NAME=Cell-KN-Phenotypes
$ export ARANGO_SCHEMA_DB_NAME=Cell-KN-Schema
$ export ARANGO_ONTOLOGY_GRAPH_NAME=KN-Ontologies-v2.0
$ export ARANGO_PHENOTYPE_GRAPH_NAME=KN-Phenotypes-v2.0
$ cd src/main/shell
$ ./start-arangodb.sh
```

### Neo4j

A Neo4j docker image can be downloaded and a container started as follows:
```
$ export NEO4J_HOME="<some-path>/neo4j"
$ export NEO4J_PASSWORD="<some-password>"
$ cd src/main/shell
$ ./start-neo4j.sh
```
The Neo4j browser is exposed at `http://localhost:7474` and the Bolt
endpoint at `bolt://localhost:7687`. Once an ArangoDB download has been
produced (see `download-arangodb.sh`), the resulting TSV files can be
loaded into Neo4j via:
```
$ ./upload-neo4j.sh
```

### Apache Jena (TDB2)

An Apache Jena Fuseki docker image (with a TDB2 backend) can be downloaded
and a container started as follows:
```
$ export JENA_HOME="<some-path>/jena"
$ export JENA_PASSWORD="<some-password>"
$ cd src/main/shell
$ ./start-jena.sh
```
The Fuseki admin UI and SPARQL endpoint are exposed at
`http://localhost:3030`. Once an ArangoDB download has been produced (see
`download-arangodb.sh`), the resulting TSV files can be transformed to
N-Triples and loaded into the Fuseki dataset via:
```
$ ./upload-jena.sh
```

### Java

Java SE 21 and Maven 3 or compatible are required to generate the Javadocs,
test, and package. From the repository root directory run:
```
$ mvn javadoc:javadoc
$ mvn test
$ mvn clean package -DskipTests
```
The Javadoc output is written to `docs/java/` (gitignored), as configured by
the `maven-javadoc-plugin` in `pom.xml`.

### Data

The Python and Java classes require the ontology files to reside in
`data/obo`. The [`pipeline.py`](#usage) flow does this automatically in
Phase 1, so this step is only needed for standalone Java- or Python-only
development. From the repository root directory you can populate this directory
as follows:
```
$ export CP="target/nlm-ckn-etl-1.0.jar"
$ java -cp $CP gov.nih.nlm.OntologyDownloader
$ java -cp $CP gov.nih.nlm.OntologySlimmer
```

The Python classes also require data in the
[nlm-ckn](https://github.com/NIH-NLM/nlm-ckn) repository to be
accessible. Clone this repository at the same level as this repository.

### Python

Python 3.12 and Poetry are required to generate the Sphinx documentation, test,
and run.

Two of the Python dependencies are fetched from GitHub over SSH, so a GitHub
SSH key must be configured before running `poetry install`. See
[Connecting to GitHub with SSH](https://docs.github.com/en/authentication/connecting-to-github-with-ssh)
if you have not set one up. One of those dependencies (`kgx`) points at a
personal fork that carries a patch not yet accepted upstream; it will be
switched back to the canonical repository once the upstream PR is merged.

From the repository root directory you can install the dependencies as
follows:
```
$ cd python
$ python3.12 -m venv .poetry
$ source .poetry/bin/activate
$ python -m pip install -r .poetry.txt
$ deactivate
$ python3.12 -m venv .venv
$ source .venv/bin/activate
$ .poetry/bin/poetry install
```
From the repository root directory generate the Sphinx documentation as
follows:
```
$ cd docs/python
$ make clean html
```
From the repository root directory run Python tests as follows:
```
$ cd python/tests
$ python -m pytest *.py
```

## CI/CD Workflows

The `.github/workflows/` directory contains the following GitHub Actions workflows:

### `ci.yml` — CI (tests)

Triggered on `push` and `pull_request` for source and build file changes. Runs three jobs in parallel:

- **Java tests** — delegates to the reusable `java-build.yml` workflow (Maven test suite)
- **Python lint and tests** — delegates to the reusable `python-lint.yml` workflow (ruff + pytest, with ArangoDB started via Docker)
- **Pipeline image smoke test** — an inline job that builds the pipeline Docker image locally (no ECR push) and verifies that the production Python modules import successfully, catching missing dependencies or Dockerfile regressions

Also callable as a reusable workflow (`workflow_call`) by `on-release.yml`.

### `java-build.yml` — Java Build (reusable)

Reusable (`workflow_call`) workflow that runs the Maven test suite for fast PR
feedback. Invoked by `ci.yml`. The JAR is no longer stored as a GitHub Actions
artifact — it is compiled inside Docker by the `java-build` stage of the
`Dockerfile` and baked into the pipeline image (see `build-image.yml`).

### `python-lint.yml` — Python Lint and Test (reusable)

Reusable (`workflow_call`) workflow that runs ruff and pytest against the Python
sources (with ArangoDB started via Docker). Invoked by `ci.yml`.

### `build-image.yml` — Build and Push Docker Images

Triggered via `workflow_call` (used by `on-release.yml`) or `workflow_dispatch` for manual builds. Not triggered on push — images are only built as part of a release or on demand.

Builds two images and pushes them to ECR:
- **pipeline** (`nlm-ckn-etl-pipeline`) — JRE + compiled JAR + Python source; used by the release Batch job
- **fetcher** (`nlm-ckn-etl-fetcher`) — Python only; used by the fetch Batch job

Images are tagged `:{branch-name}` on every branch and `:latest` on `main`.

### `on-release.yml` — On Release: Trigger ETL

Triggered when a GitHub Release is published. Enforces the full gate before submitting a job:

```
test (ci.yml) → build (build-image.yml) → submit Batch job
```

The Batch job runs `release.py` end-to-end (fetch + full ETL) on EC2 and returns immediately. Progress is tracked via a [GitHub Deployment](https://docs.github.com/en/rest/deployments) — the Batch container posts `success` or `failure` to the deployment status when the pipeline finishes, which triggers a notification in any Slack channel subscribed to the GitHub app's deployment events.

Release settings (`nlm_ckn_tag`, `tar_source`, etc.) come from `release.json` at the repo root, not from this repo's release tag.

Required secrets: `AWS_RELEASE_ROLE_ARN`, `AWS_REGION`, `S3_BUCKET`, `DEPLOYMENTS_TOKEN` (classic PAT with `repo:deployments` scope).

### `on-release-deactivate.yml` — On Release Removed: Delete Deployment

Triggered when a GitHub Release is `deleted` or `unpublished`. Finds any GitHub deployment for the release's tag/ref in the `production` environment, marks it inactive (GitHub requires no active status before deletion), then deletes it so the deployments page stays clean. Requires `DEPLOYMENTS_TOKEN` (PAT with `deployments:write` scope).

### `trigger-release.yml` — Manual Release Trigger

Allows manually submitting a Batch release job via `workflow_dispatch` without publishing a full GitHub Release. Accepts the same parameters as `on-release.yml`.

### `promote-to-upstream.yml` — Promote to Upstream

Promotes a completed ETL run to the upstream `nlm-ckn` repository.

### Workflow dependency graph

```
push/PR
  └── ci.yml
        ├── java-build.yml  (Java tests)
        ├── python-lint.yml (Python lint/tests)
        └── smoke-test      (pipeline image import check)

GitHub Release published
  └── on-release.yml
        ├── ci.yml          (tests → java-build.yml + python-lint.yml)
        ├── build-image.yml (image build, needs: test)
        └── submit Batch job (needs: build)
              └── Batch container → posts deployment status to GitHub

GitHub Release deleted/unpublished
  └── on-release-deactivate.yml (mark deployment inactive → delete)
```

## Usage

The pipeline is orchestrated by three Prefect flows in `python/src/flows/`,
which wrap the individual Java and Python programs shown in the diagram above.
Each flow can run standalone or as part of a full release. See
[`python/src/flows/README.md`](python/src/flows/README.md) for the S3 layout,
lifecycle, and per-phase read/write details.

| Flow | Role |
|------|------|
| `fetch.py` | Download raw data from CELLxGENE, Open Targets, NCBI Gene, UniProt, and HuBMAP into a local cache (and optionally S3) |
| `pipeline.py` | Three-phase ETL: build the ontology graph (Phase 1), write tuples and build the results graph (Phase 2), and dump the golden archive (Phase 3) |
| `release.py` | End-to-end release from an nlm-ckn GitHub tag: extract results, refresh the cache via `fetch.py`, then run the full `pipeline.py` |

### Prerequisites

Run the flows from the `python/` directory with ArangoDB running and the
Python environment activated:
```
$ docker compose up -d                  # start ArangoDB (from the repo root)
$ cd python
$ source .venv/bin/activate
```
Ensure the ArangoDB environment variables from the [ArangoDB](#arangodb)
section above are exported, and provide NCBI E-Utilities credentials either as
environment variables or via the `--ncbi-email` / `--ncbi-api-key` flags:
```
$ export NCBI_EMAIL="<some-email>"
$ export NCBI_API_KEY="<some-api-key>"
```
`S3_BUCKET` is optional — omit it to work entirely on local disk.

### Running the flows

**Full release** (extract results → fetch → three-phase ETL) from an nlm-ckn tag:
```
$ poetry run src/flows/release.py \
    --nlm-ckn-tag v0.0.2 \
    --ncbi-email "$NCBI_EMAIL" \
    --ncbi-api-key "$NCBI_API_KEY"
```
By default `release.py` reuses a warm external cache when it is younger than
`--max-fetch-age-hours` (default 672 h = four weeks) and only forces a full
re-fetch when the cache is missing, stale, or produced by changed fetch code.

**Fetch only** (refresh the external cache without a full release):
```
$ poetry run src/flows/fetch.py \
    --ncbi-email "$NCBI_EMAIL" \
    --ncbi-api-key "$NCBI_API_KEY"
```
Add `--force` to ignore the cache and re-fetch every source from scratch, or
`--retry-empty` to retry only previously-failed entries.

**Pipeline only** (external cache and results already present locally or in S3).
The phase flags are independent, so you can run any subset:
```
# Phase 2 only — reuses the existing Phase 1 baseline dump
$ poetry run src/flows/pipeline.py --run-results --run-name 2026-04

# All three phases
$ poetry run src/flows/pipeline.py \
    --run-ontology --run-results --run-archive --run-name 2026-04
```
| `pipeline.py` flag | Phase | Effect |
|--------------------|-------|--------|
| `-o` / `--run-ontology` | 1 | Build the ontology graph and save a baseline dump (skipped if the dump exists) |
| `-r` / `--run-results` | 2 | Restore the baseline, write tuples, build the results graph, create analyzers/views, dump results |
| `-a` / `--run-archive` | 3 | Build the induced phenotype subgraph, create phenotype analyzers/views, dump the golden artifact, promote to S3 |

Each phase is skipped automatically when its output already exists; the
uppercase variants (`-O` / `-R` / `-A`) force the phase to re-run regardless.
