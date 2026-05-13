#!/usr/bin/env bash
# release-entrypoint.sh — entrypoint for the AWS Batch release job.
#
# Runs flows/release.py, which internally handles:
#   1. Downloading (or reading) the nlm-ckn release tarball → data/results-<run>/
#   2. Fetching all external APIs (CELLxGENE, Open Targets, NCBI, UniProt)
#   3. Three-phase ETL: ontology graph → results/graphs → golden S3 dump
#
# Required environment variables (injected by the Batch job definition):
#   CELL_KN_TAG    — nlm-ckn release tag, e.g. v2026-04
#   S3_BUCKET      — S3 bucket for cache, dumps, and run artifacts
#   NCBI_EMAIL     — NCBI E-Utilities email address
#   NCBI_API_KEY   — NCBI E-Utilities API key
#   RELEASE_CONFIG — S3 URL for release.json (uploaded by trigger-release.sh)
#
# Optional environment variables (set via container overrides at submit time):
#   TAR_SOURCE          — override tarball URL or S3 path (default: derived from tag)
#   RUN_NAME            — ETL run name (default: tag with leading 'v' stripped)
#   SKIP_ONTOLOGY       — set to 'true' to reuse an existing baseline dump
#   MAX_FETCH_AGE_HOURS — max external cache age before forcing a re-fetch (default: 48)
#   JAVA_OPTS           — JVM flags (default: -Xmx32g)
#   GITHUB_TOKEN        — GitHub token for private repos or to avoid rate limits
set -euo pipefail

: "${CELL_KN_TAG:?CELL_KN_TAG must be set}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${NCBI_EMAIL:?NCBI_EMAIL must be set}"
: "${NCBI_API_KEY:?NCBI_API_KEY must be set}"
: "${RELEASE_CONFIG:?RELEASE_CONFIG must be set}"

echo "=== Starting release: tag=${CELL_KN_TAG} ==="

args=(
  --tag            "${CELL_KN_TAG}"
  --ncbi-email     "${NCBI_EMAIL}"
  --ncbi-api-key   "${NCBI_API_KEY}"
  --release-config "${RELEASE_CONFIG}"
)

[[ -n "${TAR_SOURCE:-}"           ]] && args+=(--tar-source         "${TAR_SOURCE}")
[[ -n "${RUN_NAME:-}"             ]] && args+=(--run-name            "${RUN_NAME}")
[[ -n "${MAX_FETCH_AGE_HOURS:-}"  ]] && args+=(--max-fetch-age-hours "${MAX_FETCH_AGE_HOURS}")
[[ -n "${JAVA_OPTS:-}"            ]] && args+=(--java-opts           "${JAVA_OPTS}")
[[ "${SKIP_ONTOLOGY:-}" == "true" ]] && args+=(--skip-ontology)

exec python /app/python/src/flows/release.py "${args[@]}"
