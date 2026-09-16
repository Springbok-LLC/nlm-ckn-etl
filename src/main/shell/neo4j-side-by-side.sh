#!/usr/bin/env bash
# Build the read-only Neo4j dataset image two ways and run both side by side:
#
#   kgx   from the run's KGX export     (rmyung/kgx-to-neo4j)
#         http://localhost:7474  bolt://localhost:7687
#   dump  from the run's golden dump    (rmyung/neo4j-from-golden-dump)
#         http://localhost:7475  bolt://localhost:7688
#
# Everything is pulled fresh, so it runs from a plain clone of
# Springbok-LLC/nlm-ckn-etl:
#   - both branches are fetched from the Springbok-LLC remote, and each image is
#     built from its branch's Dockerfile and converter (git archive), whatever
#     is checked out
#   - runs/<run>/07-kgx.tar.gz and runs/<run>/06-golden-dump.tar.gz are synced
#     from S3 into a cache directory (only re-downloaded when they change)
#   - images are tagged nlm-ckn-etl-neo4j:<kgx|dump>-<run>-<commit>-<data>, so
#     a new commit or a re-uploaded tarball builds a new image and anything
#     else reuses the existing one
#
# The containers get the ECS task definition's settings (no auth, read-only,
# memory limits, 1 CPU / 4 GB each), except that HTTP is open for Neo4j
# Browser. Ports are bound to 127.0.0.1 only, since there is no auth.
#
# Requirements:
#   - git, with read access to Springbok-LLC/nlm-ckn-etl
#   - Docker 23+ with buildx, and about 8 GB of memory for Docker
#   - AWS CLI v2 with read access to the ETL bucket, e.g. AWS_PROFILE=springbok
#
# Once both are healthy, it prints where the two differ: node and relationship
# counts, a pair of parallel edges, a node whose id KGX merged away, and
# whether list values stayed lists.
#
# Usage:
#   src/main/shell/neo4j-side-by-side.sh up [run]   default run: v1.7.0-rc.2
#   src/main/shell/neo4j-side-by-side.sh compare    reprint the comparison
#   src/main/shell/neo4j-side-by-side.sh down
#
# Optional environment:
#   S3_BUCKET    ETL data bucket (default nlm-ckn-arangodb-data-952291113202)
#   KGX_BRANCH   default rmyung/kgx-to-neo4j
#   DUMP_BRANCH  default rmyung/neo4j-from-golden-dump
#   REMOTE       git remote to fetch from (default: the one pointing at
#                Springbok-LLC/nlm-ckn-etl)
#   CACHE_DIR    downloads (default ~/.cache/nlm-ckn-neo4j-side-by-side)
set -euo pipefail

S3_BUCKET=${S3_BUCKET:-nlm-ckn-arangodb-data-952291113202}
KGX_BRANCH=${KGX_BRANCH:-rmyung/kgx-to-neo4j}
DUMP_BRANCH=${DUMP_BRANCH:-rmyung/neo4j-from-golden-dump}
CACHE_DIR=${CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/nlm-ckn-neo4j-side-by-side}
RUN=${2:-v1.7.0-rc.2}

HEALTH_CMD="wget -q -O - --header='Content-Type: application/json' \
--post-data='{\"statement\":\"MATCH (n) RETURN count(n) > 0 AS ok\"}' \
http://127.0.0.1:7474/db/neo4j/query/v2 | grep -q '\[true\]'"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

check_requirements() {
  command -v git >/dev/null || die "git is not installed"
  command -v docker >/dev/null || die "docker is not installed"
  command -v aws >/dev/null || die "the AWS CLI is not installed"
  docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon; is Docker running?"
  docker buildx version >/dev/null 2>&1 || die "docker buildx is not available"
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die "no AWS credentials; log in and/or set AWS_PROFILE (e.g. AWS_PROFILE=springbok)"
  local mem
  mem=$(docker info --format '{{.MemTotal}}')
  if [ "$mem" -lt $((6 * 1024 * 1024 * 1024)) ]; then
    echo "WARNING: Docker has $((mem / 1024 / 1024)) MiB of memory; the two containers need about 5 GiB." >&2
  fi
}

# Fetch both branches from the Springbok-LLC remote into its remote-tracking refs.
fetch_branches() {
  if [ -z "${REMOTE:-}" ]; then
    REMOTE=$(git -C "$REPO" remote -v \
      | awk '$3 == "(fetch)" && $2 ~ /Springbok-LLC\/nlm-ckn-etl(\.git)?$/ { print $1; exit }')
    [ -n "$REMOTE" ] || die "no git remote points at Springbok-LLC/nlm-ckn-etl; set REMOTE"
  fi
  local branch
  for branch in "$KGX_BRANCH" "$DUMP_BRANCH"; do
    echo "Fetching $REMOTE/$branch" >&2
    git -C "$REPO" fetch --quiet --no-tags "$REMOTE" \
      "+refs/heads/$branch:refs/remotes/$REMOTE/$branch"
  done
}

# Sync the run's two tarballs and extract just the Cell-KN-Ontologies inputs.
fetch_data() {
  local dir="$CACHE_DIR/$RUN"
  mkdir -p "$dir"
  echo "Syncing s3://$S3_BUCKET/runs/$RUN/{07-kgx,06-golden-dump}.tar.gz to $dir" >&2
  aws s3 sync --only-show-errors "s3://$S3_BUCKET/runs/$RUN/" "$dir/" \
    --exclude '*' --include 07-kgx.tar.gz --include 06-golden-dump.tar.gz
  [ -f "$dir/07-kgx.tar.gz" ] || die "s3://$S3_BUCKET/runs/$RUN/07-kgx.tar.gz not found"
  [ -f "$dir/06-golden-dump.tar.gz" ] || die "s3://$S3_BUCKET/runs/$RUN/06-golden-dump.tar.gz not found"

  rm -rf "$dir/kgx" "$dir/dump"
  mkdir "$dir/kgx" "$dir/dump"
  tar -xzf "$dir/07-kgx.tar.gz" --strip-components=1 -C "$dir/kgx" \
    "kgx-$RUN/Cell-KN-Ontologies_nodes.tsv" "kgx-$RUN/Cell-KN-Ontologies_edges.tsv"
  tar -xzf "$dir/06-golden-dump.tar.gz" --strip-components=2 -C "$dir/dump" \
    "arangodump-golden-$RUN/Cell-KN-Ontologies"
}

build() {  # name branch tarball
  local name=$1 branch=$2 tarball=$3 ref image src
  ref="$REMOTE/$branch"
  image="nlm-ckn-etl-neo4j:${name}-${RUN}-$(git -C "$REPO" rev-parse --short "$ref")"
  image="${image}-$(git hash-object "$CACHE_DIR/$RUN/$tarball" | cut -c1-7)"
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "Reusing $image" >&2
  else
    src=$(mktemp -d)
    git -C "$REPO" archive "$ref" python/src src/main/docker/neo4j | tar -x -C "$src"
    echo "Building $image from $ref" >&2
    # The named build context is `kgx` on one branch and `dump` on the other.
    docker buildx build --provenance=false --load \
      -f "$src/src/main/docker/neo4j/Dockerfile" \
      --build-context "$name=$CACHE_DIR/$RUN/$name" \
      -t "$image" "$src/python/src" >&2
    rm -rf "$src"
  fi
  echo "$image"
}

start() {  # name image http-port bolt-port
  local name=$1 image=$2 http=$3 bolt=$4
  docker rm -f "neo4j-$name" >/dev/null 2>&1 || true
  docker run -d --name "neo4j-$name" --cpus 1 --memory 4g \
    -p "127.0.0.1:$http:7474" -p "127.0.0.1:$bolt:7687" \
    -e NEO4J_AUTH=none \
    -e NEO4J_server_databases_default__to__read__only=true \
    -e NEO4J_server_memory_heap_initial__size=1536m \
    -e NEO4J_server_memory_heap_max__size=1536m \
    -e NEO4J_server_memory_pagecache_size=1g \
    -e NEO4J_db_memory_transaction_total_max=1g \
    -e NEO4J_db_memory_transaction_max=256m \
    -e NEO4J_db_transaction_timeout=30s \
    -e NEO4J_internal_dbms_cypher__ip__blocklist='0.0.0.0/0,::/0' \
    -e NEO4J_dbms_security_allow__csv__import__from__file__urls=false \
    -e NEO4J_dbms_usage__report_enabled=false \
    -e NEO4J_server_http_listen__address=0.0.0.0:7474 \
    -e NEO4J_server_bolt_advertised__address="localhost:$bolt" \
    --health-cmd "$HEALTH_CMD" --health-interval 5s --health-timeout 10s \
    --health-retries 3 --health-start-period 120s \
    "$image" >/dev/null
}

# Where the KGX export and the golden dump are known to differ (v1.7.0-rc.2):
# KGX keeps one edge per subject, collection and object, takes node ids from
# stale oboInOwl:id annotations (merging oRGC4 into oRGC1), and joins lists.
# Each entry is "row label|query"; every query returns one value, aliased v.
COMPARISONS=(
  "Nodes|MATCH (n) RETURN count(n) AS v"
  "Relationships|MATCH ()-[r]->() RETURN count(r) AS v"
  "CL:0000626 -> CL:1001502 Labels|MATCH (:\`biolink:NamedThing\` {id: 'CL:0000626'})-[r]->(:\`biolink:NamedThing\` {id: 'CL:1001502'}) WITH r.Label AS label ORDER BY label WITH collect(label) AS labels RETURN CASE size(labels) WHEN 0 THEN '(none)' ELSE reduce(s = head(labels), l IN tail(labels) | s + ', ' + l) END AS v"
  "CL:0020041 (oRGC4)|OPTIONAL MATCH (n:\`biolink:NamedThing\` {id: 'CL:0020041'}) RETURN CASE WHEN n IS NULL THEN 'missing' ELSE 'present: ' + n.label END AS v"
  "HP:0012871 hasExactSynonym|MATCH (n:\`biolink:NamedThing\` {id: 'HP:0012871'}) RETURN valueType(n.hasExactSynonym) AS v"
  "HP nodes with list hasExactSynonym|MATCH (n:HP) WHERE n.hasExactSynonym IS NOT NULL RETURN count(CASE WHEN valueType(n.hasExactSynonym) STARTS WITH 'LIST' THEN 1 END) + ' of ' + count(n) AS v"
)

# Print one value per comparison, running every query in a single cypher-shell
# session (each session starts a JVM). bolt://, not the default neo4j://:
# routing would follow the advertised address, the host's port.
query_all() {  # name
  local row
  for row in "${COMPARISONS[@]}"; do
    printf '%s;\n' "${row#*|}"
  done | docker exec -i "neo4j-$1" cypher-shell -a bolt://localhost:7687 --format plain 2>/dev/null \
    | grep -vx v | sed 's/^"//; s/"$//'
}

compare() {
  local name
  for name in kgx dump; do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "neo4j-$name" 2>/dev/null)" = healthy ] \
      || die "neo4j-$name is not running and healthy; run '$0 up' first"
  done
  local kgx dump row i=0 format="%-36s  %-24s  %s\n"
  kgx=$(query_all kgx || true)
  dump=$(query_all dump || true)
  echo
  # shellcheck disable=SC2059
  printf "$format" "" "kgx (7474 / 7687)" "dump (7475 / 7688)"
  for row in "${COMPARISONS[@]}"; do
    i=$((i + 1))
    # shellcheck disable=SC2059
    printf "$format" "${row%%|*}" "$(nth_line "$kgx" "$i")" "$(nth_line "$dump" "$i")"
  done
}

nth_line() {  # text n
  local line
  line=$(printf '%s\n' "$1" | sed -n "${2}p")
  echo "${line:-(query failed)}"
}

wait_healthy() {  # name
  local status
  for _ in $(seq 1 90); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "neo4j-$1")
    [ "$status" = starting ] || break
    sleep 2
  done
  echo "neo4j-$1: $status"
  if [ "$status" != healthy ]; then
    docker logs --tail 30 "neo4j-$1" >&2
    return 1
  fi
}

case "${1:-}" in
  up)
    [[ "$RUN" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die "invalid run name: $RUN"
    REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
    check_requirements
    fetch_branches
    fetch_data
    kgx_image=$(build kgx "$KGX_BRANCH" 07-kgx.tar.gz)
    dump_image=$(build dump "$DUMP_BRANCH" 06-golden-dump.tar.gz)
    start kgx "$kgx_image" 7474 7687
    start dump "$dump_image" 7475 7688
    wait_healthy kgx
    wait_healthy dump
    echo
    echo "kgx   $kgx_image"
    echo "      http://localhost:7474  bolt://localhost:7687"
    echo "dump  $dump_image"
    echo "      http://localhost:7475  bolt://localhost:7688"
    compare
    ;;
  compare)
    compare
    ;;
  down)
    docker rm -f neo4j-kgx neo4j-dump
    ;;
  *)
    echo "Usage: $0 up [run] | compare | down" >&2
    exit 2
    ;;
esac
