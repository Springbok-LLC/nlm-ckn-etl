#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_DB_PASSWORD:?ARANGO_DB_PASSWORD must be set (see .env.example)}"
: "${ARANGO_DB_HOME:?ARANGO_DB_HOME must be set (see .env.example)}"
container_id=$(docker ps -q -f "name=^arangodb$")
if [ -z "$container_id" ]; then
    mkdir -p $ARANGO_DB_HOME
    docker run \
           --name arangodb \
           --restart always \
           -e ARANGO_ROOT_PASSWORD=${ARANGO_DB_PASSWORD} \
           -p 8529:8529 \
           -d \
           -v ${ARANGO_DB_HOME}:/var/lib/arangodb3 \
           arangodb/arangodb:latest > /dev/null
fi
