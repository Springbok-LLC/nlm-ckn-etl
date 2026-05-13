# Dockerfile — multi-stage build for the NLM-CKN ETL
#
# Three stages:
#   base     — shared Python stack (no Java, no entrypoint)
#   fetcher  — ECS Fargate scheduled fetch task (fetch-entrypoint, no Java)
#   pipeline — AWS Batch ETL job (adds JRE for Java graph builders)
#
# ── Local usage ───────────────────────────────────────────────────────────────
#
#   ArangoDB is managed externally — the pipeline container does NOT start it.
#   Start ArangoDB first via compose.yaml, then run the pipeline image against it.
#
#   Build pipeline image:
#     docker build --target pipeline -t nlm-ckn-etl-pipeline .
#
#   Step 1 — start ArangoDB (compose.yaml manages the data volume and password):
#     ARANGO_DB_PASSWORD=<password> docker compose up -d arangodb
#
#   Step 2 — run the full release (Docker socket needed for arangodump/arangorestore):
#     docker run --rm \
#       --network host \
#       -v "$(pwd)/data:/app/data" \
#       -v "$(pwd)/target:/app/target" \
#       -v /var/run/docker.sock:/var/run/docker.sock \
#       -e S3_BUCKET=<bucket> \
#       -e CELL_KN_TAG=v2026-04 \
#       -e NCBI_EMAIL=<email> -e NCBI_API_KEY=<key> \
#       nlm-ckn-etl-pipeline
#
#   To run a single ETL phase locally (bypasses release-entrypoint):
#     docker run --rm --entrypoint python \
#       --network host \
#       -v "$(pwd)/data:/app/data" \
#       -v "$(pwd)/target:/app/target" \
#       -v /var/run/docker.sock:/var/run/docker.sock \
#       -e ARANGO_DB_PASSWORD=<password> \
#       nlm-ckn-etl-pipeline \
#       /app/python/src/flows/pipeline.py --run-results
#
#   NOTE: --network host lets the container reach ArangoDB on localhost:8529.
#   The Docker socket is only needed for arangodump/arangorestore (exec into
#   the ArangoDB container); it is not used to start or stop ArangoDB.
#
#   Build fetcher image:
#     docker build --target fetcher -t nlm-ckn-etl-fetcher .
#
#   Run the fetch flow only (writes to data/external/):
#   NOTE: fetcher.py fetches data from external APIs (CELLxGENE, Open Targets,
#   NCBI Gene, UniProt, HuBMAP) via ExternalApiResultsFetcher.py.  Use --memory 8g
#   (or raise Docker Desktop's memory limit) to avoid an OOM kill.
#     docker run --rm \
#       --memory 8g \
#       -v "$(pwd)/data:/app/data" \
#       -e NCBI_EMAIL=<email> -e NCBI_API_KEY=<key> \
#       nlm-ckn-etl-fetcher
#
#   Run the scheduled fetch with S3 sync (mirrors ECS Fargate):
#     docker run --rm \
#       --memory 8g \
#       -e S3_BUCKET=<bucket> \
#       -e NCBI_EMAIL=<email> -e NCBI_API_KEY=<key> \
#       nlm-ckn-etl-fetcher
#
# ── AWS usage ────────────────────────────────────────────────────────────────
#   Both images are pushed to ECR by .github/workflows/build-image.yml.
#   nlm-ckn-etl-fetcher  → used by the ECS Fargate task (cloudformation/fetch.yaml)
#   nlm-ckn-etl-pipeline → used by the AWS Batch job   (cloudformation/batch.yaml)
#                          Entrypoint runs release.py (fetch + full ETL).
#                          Trigger via src/main/shell/trigger-release.sh.
#
#   compose.yaml runs the arangodb service locally for development.

# ── Stage 0: Java build ──────────────────────────────────────────────────────
# Compiles the ETL JAR inside Docker so the pipeline image is self-contained —
# no S3 download needed at runtime.  The maven cache mount keeps downloaded
# dependencies across builds so only changed code triggers a re-download.
FROM --platform=linux/amd64 maven:3.9-eclipse-temurin-21 AS java-build

WORKDIR /build
COPY pom.xml .
# Resolve dependencies in a separate layer so they are cached independently
# of source changes (rebuilds after a source-only change skip this layer).
RUN --mount=type=cache,target=/root/.m2 \
    mvn dependency:go-offline -q

COPY src/main/java ./src/main/java
RUN --mount=type=cache,target=/root/.m2 \
    mvn package -DskipTests -q


# ── Stage 1: base (shared Python stack) ─────────────────────────────────────
# Pin to linux/amd64: scikit-misc has no linux/arm64 binary wheel on PyPI.
# amd64 wheels run correctly under Rosetta on Apple Silicon.
FROM --platform=linux/amd64 python:3.12-slim AS base

# ── System build deps (needed by scikit-misc, scanpy, h5py, etc.) ──────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gfortran \
        libhdf5-dev \
        liblapack-dev \
        libblas-dev \
    && rm -rf /var/lib/apt/lists/*

# ── uv (fast Python installer) ─────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_SYSTEM_PYTHON=1

# ── Prefect runtime settings ───────────────────────────────────────────────
# PREFECT_HOME on tmpfs (/tmp) keeps the ephemeral SQLite API database in
# memory-backed storage, which is faster than disk — important when running
# under Rosetta emulation (amd64 image on Apple Silicon) where disk I/O is
# slow enough to trigger the default 20-second startup timeout.
ENV PREFECT_HOME=/tmp/prefect
# Raise the ephemeral API server startup timeout to 120 seconds.
# The default (20 s) is tight for Rosetta-emulated containers; this is a
# no-op cost on native amd64 hardware where the server starts in < 5 s.
ENV PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS=120
# Disable Prefect's background telemetry heartbeat — it races the ephemeral
# SQLite server at startup and causes "database is locked" errors.
ENV PREFECT_TELEMETRY_ENABLED=false

# ── Python dependencies ────────────────────────────────────────────────────
# poetry is used only to export the pinned requirements from poetry.lock;
# uv handles the actual install (parallel downloads, ~10-100x faster than pip).
RUN pip install --no-cache-dir poetry==2.3.2 \
    && poetry self add poetry-plugin-export

WORKDIR /app

COPY python/pyproject.toml python/poetry.lock python/

RUN --mount=type=cache,target=/root/.cache/uv \
    cd python \
    && poetry export --without dev --without-hashes -f requirements.txt -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt

# ── AWS CLI ────────────────────────────────────────────────────────────────
# Used by S3 sync tasks in fetcher.py and pipeline.py.
RUN pip install --no-cache-dir awscli

# ── Application source ────────────────────────────────────────────────────
COPY python/src /app/python/src

# ── Static config data files ───────────────────────────────────────────────
# Small repo-tracked files read at runtime (NSForest source lists, mappings).
# Large generated data (obo/, external/, results/, tuples/) must be mounted or
# synced from S3 at runtime.
COPY data/*.json data/*.csv /app/data/
# data/obo/ files are generated at runtime:
#   deprecated_terms.txt and edge_labels.txt are WRITTEN by OntologyGraphBuilder
#   during --run-ontology; *.owl files are downloaded by OntologyDownloader.
# Create an empty placeholder for deprecated_terms.txt so that LoaderUtilities.py
# (which opens it at module import time) doesn't crash with FileNotFoundError
# when the pipeline container starts before --run-ontology has run.
RUN mkdir -p /app/data/obo && touch /app/data/obo/deprecated_terms.txt


# ── Stage 2: fetcher (ECS Fargate scheduled fetch task) ─────────────────────
# No Java. Entrypoint syncs S3 → runs fetcher.py → syncs back.
FROM base AS fetcher

COPY src/main/shell/fetch-entrypoint.sh /usr/local/bin/fetch-entrypoint
RUN chmod +x /usr/local/bin/fetch-entrypoint

ENTRYPOINT ["/usr/local/bin/fetch-entrypoint"]


# ── Stage 3: pipeline (AWS Batch ETL job) ────────────────────────────────────
# Adds JRE so release.py can call Java graph builders via subprocess.
# openjdk-21-jre-headless matches the JDK 21 used to compile the JAR
# (see build-jar.yml).
#
# The default entrypoint runs the full release flow (release-entrypoint).
# For local debugging of a single phase, override the entrypoint:
#   docker run --entrypoint python ... nlm-ckn-etl-pipeline \
#     /app/python/src/flows/pipeline.py --run-results
FROM base AS pipeline

RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# JAR baked in — ensure_jar finds it immediately without an S3 download.
# The content hash (jar_key) is still computed at runtime to key baseline dumps.
COPY --from=java-build /build/target/nlm-ckn-etl-1.0.jar /app/target/nlm-ckn-etl-1.0.jar

COPY src/main/shell/release-entrypoint.sh /usr/local/bin/release-entrypoint
RUN chmod +x /usr/local/bin/release-entrypoint

WORKDIR /app
ENTRYPOINT ["/usr/local/bin/release-entrypoint"]
