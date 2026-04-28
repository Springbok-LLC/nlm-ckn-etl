#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_PHENOTYPE_DB_NAME:?ARANGO_PHENOTYPE_DB_NAME must be set (see .env.example)}"
: "${ARANGO_DB_PASSWORD:?ARANGO_DB_PASSWORD must be set (see .env.example)}"
port="${1:-8529}"
http_code=$(curl -sS -o /dev/null -w "%{http_code}" \
    -u "root:${ARANGO_DB_PASSWORD}" \
    -H "Content-Type: application/json" \
    -X POST "http://localhost:${port}/_db/_system/_api/database" \
    -d "{\"name\":\"${ARANGO_PHENOTYPE_DB_NAME}\"}")
if [[ "$http_code" != "201" && "$http_code" != "409" ]]; then
    echo "Failed to create database ${ARANGO_PHENOTYPE_DB_NAME} (HTTP ${http_code})"
    exit 1
fi
kgx arangodb-upload \
    -l "http://localhost:${port}" \
    -d "${ARANGO_PHENOTYPE_DB_NAME}" \
    -u root \
    -p "${ARANGO_DB_PASSWORD}" \
    -i tsv \
    "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}_nodes.tsv" \
    "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}_edges.tsv" \
    --curie-routing
