#!/usr/bin/env bash
set -euo pipefail
: "${ARANGO_DB_PASSWORD:?ARANGO_DB_PASSWORD must be set (see .env.example)}"
: "${ARANGO_DB_HOME:?ARANGO_DB_HOME must be set (see .env.example)}"
port="${1:-8529}"
if [[ "$port" == "8529" ]]; then
    name="arangodb"
    home="${ARANGO_DB_HOME}"
else
    name="arangodb-${port}"
    home="${ARANGO_DB_HOME}-${port}"
fi
container_id=$(docker ps -q -f "name=^${name}$")
if [ -z "$container_id" ]; then
    mkdir -p "$home"
    docker run \
           --name "$name" \
           --restart always \
           -e ARANGO_ROOT_PASSWORD="${ARANGO_DB_PASSWORD}" \
           -p "${port}:8529" \
           -d \
           -v "${home}":/var/lib/arangodb3 \
           arangodb/arangodb:latest > /dev/null
fi
