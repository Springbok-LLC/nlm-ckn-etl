#!/usr/bin/env bash
container_id=$(docker ps | grep neo4j | cut -d " " -f 1)
if [ -z "$container_id" ]; then
    docker run \
           --restart always \
           -e NEO4J_AUTH=neo4j/$NEO4J_PASSWORD \
           -p 7474:7474 \
           -p 7687:7687 \
           -d \
           -v $NEO4J_HOME:/data \
           neo4j:2026.03.1 > /dev/null
fi
