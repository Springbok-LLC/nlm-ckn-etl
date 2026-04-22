#!/usr/bin/env bash
db="Cell-KN-Phenotypes"
kgx arangodb-download \
    -l http://localhost:8529 \
    -d $db \
    -u root \
    -p $ARANGO_DB_PASSWORD \
    -o arangodb-download/$db \
    -f tsv \
    --all-collections
