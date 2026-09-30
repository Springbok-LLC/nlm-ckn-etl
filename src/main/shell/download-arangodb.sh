#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_PHENOTYPE_DB_NAME:?ARANGO_PHENOTYPE_DB_NAME must be set (see .env.example)}"
: "${ARANGO_DB_PASSWORD:?ARANGO_DB_PASSWORD must be set (see .env.example)}"
# Always the local container, so http unless ARANGO_DB_SCHEME says otherwise
# (the same loopback default as python/src/flows/_common.py).
kgx arangodb-download \
    -l "${ARANGO_DB_SCHEME:-http}://localhost:8529" \
    -d "${ARANGO_PHENOTYPE_DB_NAME}" \
    -u root \
    -p "${ARANGO_DB_PASSWORD}" \
    -o "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}" \
    -f tsv \
    --all-collections
