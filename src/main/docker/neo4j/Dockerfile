# syntax=docker/dockerfile:1
#
# Read-only Neo4j dataset image: one ETL run's golden ArangoDB dump, imported
# into the `neo4j` database and baked into the image. Built and pushed to the
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
# The graph model is described in ArangoDumpToNeo4jImport.py.
#
# The convert and build stages run on $BUILDPLATFORM and the final stage only
# copies files, so an arm64 image builds on an amd64 runner without emulation.
# Store files are portable across architectures.
#
# Build locally from the repo root. The context is python/src, and the named
# context `dump` is the Cell-KN-Ontologies directory of the run's golden dump:
# data/arangodump-golden-<run>/Cell-KN-Ontologies after a local pipeline run,
# or extracted from 06-golden-dump.tar.gz:
#   tar -xzf 06-golden-dump.tar.gz -C data arangodump-golden-<run>/Cell-KN-Ontologies
#   docker buildx build --platform linux/arm64 --provenance=false --load \
#     -f src/main/docker/neo4j/Dockerfile \
#     --build-context dump=data/arangodump-golden-<run>/Cell-KN-Ontologies \
#     -t nlm-ckn-etl-neo4j:<run> python/src

ARG NEO4J_VERSION=2026.03.1

# Writes /import/{nodes,relationships}/*.csv, the importer options in
# /import/import.args and the dump's totals in /import/counts.env.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS convert
COPY ArangoDumpToNeo4jImport.py InfoResUtilities.py /convert/
RUN --mount=type=bind,from=dump,target=/dump \
    python /convert/ArangoDumpToNeo4jImport.py /dump /import

# The conf edits only affect this stage: auth is off so cypher-shell can check
# the counts and create the constraint, and transaction logs are not
# preallocated, which would otherwise put a 256 MiB file of zeros per database
# in the image. `neo4j stop` (never a kill) leaves the store clean.
FROM --platform=$BUILDPLATFORM neo4j:${NEO4J_VERSION} AS build
COPY --from=convert /import /import
RUN <<'EOF'
set -eu
printf '%s\n' server.directories.data=/dataset dbms.security.auth_enabled=false \
  db.tx_log.preallocate=false >> conf/neo4j.conf
mkdir /dataset && chown neo4j:neo4j /dataset
su neo4j -s /bin/sh -c 'neo4j-admin database import full neo4j @/import/import.args'
su neo4j -s /bin/sh -c 'neo4j start'
for i in $(seq 1 60); do cypher-shell -a bolt://localhost:7687 "RETURN 1" >/dev/null 2>&1 && break; sleep 2; done
# Fail rather than bake a store with fewer nodes or relationships than the dump.
. /import/counts.env
nodes=$(cypher-shell -a bolt://localhost:7687 --format plain 'MATCH (n) RETURN count(n)' | tail -n 1)
rels=$(cypher-shell -a bolt://localhost:7687 --format plain 'MATCH ()-[r]->() RETURN count(r)' | tail -n 1)
echo "Imported ${nodes} nodes and ${rels} relationships; the dump has ${NODES} and ${RELATIONSHIPS}"
if [ "${nodes}" != "${NODES}" ] || [ "${rels}" != "${RELATIONSHIPS}" ]; then
  echo "ERROR: the import does not match the dump" >&2
  exit 1
fi
cypher-shell -a bolt://localhost:7687 'CREATE CONSTRAINT namedthing_id IF NOT EXISTS FOR (n:`biolink:NamedThing`) REQUIRE n.id IS UNIQUE; CALL db.awaitIndexes(600);'
su neo4j -s /bin/sh -c 'neo4j stop'
EOF

FROM neo4j:${NEO4J_VERSION}
COPY --from=build --chown=neo4j:neo4j /dataset /dataset
ENV NEO4J_server_directories_data=/dataset
