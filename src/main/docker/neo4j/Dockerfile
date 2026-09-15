# syntax=docker/dockerfile:1
#
# Read-only Neo4j dataset image: one ETL run's KGX export, imported into the
# `neo4j` database and baked into the image. Built and pushed to the
# nlm-ckn-etl-neo4j ECR repo, tagged with the run name, by
# .github/workflows/build-neo4j-image.yml; served by the ECS service in
# nlm-ckn-iac (environment/services/neo4j/cloudformation/neo4j.yaml).
#
# Image contract (relied on by that stack):
#   - A finished store for the `neo4j` database at /dataset, and
#     NEO4J_server_directories_data pointing at it. /data is a VOLUME in the
#     base image, so a store baked there would be discarded.
#   - The store was shut down cleanly after the id constraint was built: a
#     read-only database refuses to start if any index needs rebuilding.
#   - Nothing about server behaviour (auth, read-only, memory, LOAD CSV, HTTP
#     binding) is set here; the ECS task definition sets all of it.
#
# Only Cell-KN-Ontologies is loaded. Community allows one user database, and
# Cell-KN-Phenotypes is a strict subset of it (every node and every triple).
#
# The convert and build stages run on $BUILDPLATFORM and the final stage only
# copies files, so an arm64 image builds on an amd64 runner without emulation.
# Store files are portable across architectures.
#
# Build locally from the repo root (context is python/src; the extracted
# 07-kgx.tar.gz is the named context `kgx`):
#   mkdir -p data/kgx-<run>
#   tar -xzf 07-kgx.tar.gz --strip-components=1 -C data/kgx-<run>
#   docker buildx build --platform linux/arm64 --load \
#     -f src/main/docker/neo4j/Dockerfile --build-context kgx=data/kgx-<run> \
#     -t nlm-ckn-etl-neo4j:<run> python/src

ARG NEO4J_VERSION=2026.03.1

FROM --platform=$BUILDPLATFORM python:3.12-slim AS convert
COPY KgxToNeo4jImport.py /convert.py
COPY --from=kgx . /kgx/
RUN mkdir /import && python /convert.py /kgx /import Cell-KN-Ontologies

# The conf edits only affect this stage: auth is off so cypher-shell can create
# the constraint. `neo4j stop` (never a kill) leaves the store clean.
FROM --platform=$BUILDPLATFORM neo4j:${NEO4J_VERSION} AS build
COPY --from=convert /import /import
RUN printf 'server.directories.data=/dataset\ndbms.security.auth_enabled=false\n' >> conf/neo4j.conf \
 && mkdir /dataset && chown neo4j:neo4j /dataset \
 && su neo4j -s /bin/sh -c 'neo4j-admin database import full neo4j \
      --nodes=/import/nodes.csv --relationships=/import/edges.csv \
      --array-delimiter="|" --multiline-fields=true' \
 && su neo4j -s /bin/sh -c 'neo4j start' \
 && for i in $(seq 1 60); do cypher-shell -a bolt://localhost:7687 "RETURN 1" >/dev/null 2>&1 && break; sleep 2; done \
 && cypher-shell -a bolt://localhost:7687 'CREATE CONSTRAINT namedthing_id IF NOT EXISTS FOR (n:`biolink:NamedThing`) REQUIRE n.id IS UNIQUE; CALL db.awaitIndexes(600);' \
 && su neo4j -s /bin/sh -c 'neo4j stop'

FROM neo4j:${NEO4J_VERSION}
COPY --from=build --chown=neo4j:neo4j /dataset /dataset
ENV NEO4J_server_directories_data=/dataset
