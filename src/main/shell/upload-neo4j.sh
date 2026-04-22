#!/usr/bin/env bash
db="Cell-KN-Phenotypes"
kgx neo4j-upload \
    -l bolt://localhost:7687 \
    -u neo4j \
    -p "$NEO4J_PASSWORD" \
    -i tsv \
    arangodb-download/${db}_nodes.tsv \
    arangodb-download/${db}_edges.tsv
