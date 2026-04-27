#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_PHENOTYPE_DB_NAME:?ARANGO_PHENOTYPE_DB_NAME must be set (see .env.example)}"
: "${JENA_PASSWORD:?JENA_PASSWORD must be set (see .env.example)}"
kgx transform \
    -i tsv \
    -f nt \
    -o "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}.nt" \
    "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}_nodes.tsv" \
    "arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}_edges.tsv"
curl --fail -X POST \
     -H "Content-Type: application/n-triples" \
     --data-binary "@arangodb-download/${ARANGO_PHENOTYPE_DB_NAME}.nt" \
     -u "admin:${JENA_PASSWORD}" \
     "http://localhost:3030/${ARANGO_PHENOTYPE_DB_NAME}/data?default"
