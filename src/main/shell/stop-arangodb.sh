#!/usr/bin/env bash
set -euo pipefail
port="${1:-8529}"
if [[ "$port" == "8529" ]]; then
    name="arangodb"
else
    name="arangodb-${port}"
fi
container_id=$(docker ps -q -f "name=^${name}$")
if [ -n "$container_id" ]; then
   docker container stop "$container_id" > /dev/null
   docker container rm "$container_id" > /dev/null
fi
