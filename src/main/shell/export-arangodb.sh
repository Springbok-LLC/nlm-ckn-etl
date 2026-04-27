#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_DB_PASSWORD:?ARANGO_DB_PASSWORD must be set (see .env.example)}"
container_id=$(docker ps -q -f "name=^arangodb$")
if [ -n "$container_id" ]; then
    docker exec "$container_id" arangoexport \
        --server.endpoint tcp://127.0.0.1:8529 \
        --server.username root \
        --server.password ${ARANGO_DB_PASSWORD} \
        --server.database Cell-KN-Ontologies \
        --collection CL \
        --output-directory "exports"
    docker cp "$container_id":/exports .
fi
