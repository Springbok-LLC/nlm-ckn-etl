#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_PHENOTYPE_DB_NAME:?ARANGO_PHENOTYPE_DB_NAME must be set (see .env.example)}"
kgx neo4j-upload \
    -l bolt://localhost:7687 \
    -u neo4j \
    -p "${NEO4J_PASSWORD}" \
    -i tsv \
    "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}_nodes.tsv" \
    "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}_edges.tsv"
