#!/usr/bin/env bash
db="Cell-KN-Phenotypes"
kgx transform \
    -i tsv \
    -f nt \
    -o arangodb-download/${db}.nt \
    arangodb-download/${db}_nodes.tsv \
    arangodb-download/${db}_edges.tsv
curl -X POST \
     -H "Content-Type: application/n-triples" \
     --data-binary @arangodb-download/${db}.nt \
     -u admin:$JENA_PASSWORD \
     "http://localhost:3030/${db}/data?default"
