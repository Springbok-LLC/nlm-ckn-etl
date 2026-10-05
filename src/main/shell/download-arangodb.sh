#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_PHENOTYPE_DB_NAME:?ARANGO_PHENOTYPE_DB_NAME must be set (see .env.example)}"
: "${ARANGO_DB_PASSWORD:?ARANGO_DB_PASSWORD must be set (see .env.example)}"
# Always the local container, which serves plain HTTP, so the scheme is http
# regardless of ARANGO_DB_SCHEME (that names the scheme for ARANGO_DB_HOST,
# which this download ignores).
kgx arangodb-download \
    -l "http://localhost:8529" \
    -d "${ARANGO_PHENOTYPE_DB_NAME}" \
    -u root \
    -p "${ARANGO_DB_PASSWORD}" \
    -o "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}" \
    -f tsv \
    --all-collections
