#!/usr/bin/env bash
set -euo pipefail
container_id=$(docker ps -q -f "name=^neo4j$")
if [ -n "$container_id" ]; then
   docker container stop "$container_id" > /dev/null
   docker container rm "$container_id" > /dev/null
fi
