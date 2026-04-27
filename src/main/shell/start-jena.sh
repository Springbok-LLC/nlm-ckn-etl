#!/usr/bin/env bash
set -euo pipefail
: "${JENA_PASSWORD:?JENA_PASSWORD must be set (see .env.example)}"
: "${ARANGO_PHENOTYPE_DB_NAME:?ARANGO_PHENOTYPE_DB_NAME must be set (see .env.example)}"
: "${JENA_HOME:?JENA_HOME must be set (see .env.example)}"
container_id=$(docker ps -q -f "name=^fuseki$")
if [ -z "$container_id" ]; then
    mkdir -p "$JENA_HOME"
    docker run \
           --platform linux/amd64 \
           --name fuseki \
           --restart always \
           -e ADMIN_PASSWORD=${JENA_PASSWORD} \
           -e TDB=2 \
           -e FUSEKI_DATASET_1=${ARANGO_PHENOTYPE_DB_NAME} \
           -p 3030:3030 \
           -d \
           -v ${JENA_HOME}:/fuseki \
           stain/jena-fuseki:latest > /dev/null
fi
