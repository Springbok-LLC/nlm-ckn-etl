#!/usr/bin/env bash
set -euo pipefail
: "${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set (see .env.example)}"
: "${NEO4J_HOME:?NEO4J_HOME must be set (see .env.example)}"
container_id=$(docker ps -q -f "name=^neo4j$")
if [ -z "$container_id" ]; then
    mkdir -p $NEO4J_HOME
    docker run \
           --name neo4j \
           --restart always \
           -e NEO4J_AUTH=neo4j/${NEO4J_PASSWORD} \
           -p 7474:7474 \
           -p 7687:7687 \
           -d \
           -v ${NEO4J_HOME}:/data \
           neo4j:2026.03.1 > /dev/null
fi
