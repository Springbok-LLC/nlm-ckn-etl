#!/usr/bin/env bash
# trigger-release.sh — submit an NLM-CKN release job to AWS Batch and exit.
#
# The job runs release.py on EC2 (fetch + full ETL). This script returns
# immediately after submission; monitor progress via CloudWatch or the
# AWS Batch console.
#
# Config files loaded automatically (in order, later values win):
#   release.json  — checked-in defaults (cell_kn_tag, tar_source, hubmap_urls, …)
#   .env          — local secrets, gitignored (S3_BUCKET, AWS creds, …)
#
# Required (one of):
#   --tag TAG        nlm-ckn release tag, e.g. v2026-04
#   cell_kn_tag      set in release.json (used when --tag is omitted)
#
# Optional:
#   --tar-source PATH_OR_URL
#       Local path, S3 URL (s3://...), or HTTPS URL for the release tarball.
#       Local paths and HTTPS URLs are downloaded/staged to S3 before submission
#       so the Batch container reads from S3 only.  Set GITHUB_TOKEN to
#       authenticate HTTPS downloads from private GitHub releases.
#       Default: tar_source from release.json, or derived from --tag.
#   --skip-ontology
#       Skip Phase 1 and reuse the existing baseline dump for this run.
#   --run-name NAME
#       ETL run name (default: tag with leading 'v' stripped).
#   --max-fetch-age-hours N
#       Override the external cache age threshold (default: 48).
#   --java-opts OPTS
#       Override JVM flags (default: -Xmx32g).
#   --queue NAME
#       Batch job queue name (default: nlm-ckn-release).
#   --job-definition NAME
#       Batch job definition name (default: nlm-ckn-release).
#
# Usage:
#   bash src/main/shell/trigger-release.sh
#   bash src/main/shell/trigger-release.sh --tag v2026-04
#   bash src/main/shell/trigger-release.sh --tag v2026-04 --tar-source /path/to/prod-data-v2026-04.tar.gz
#   bash src/main/shell/trigger-release.sh --tag v2026-04 --skip-ontology

set -euo pipefail

# ── Resolve repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

RELEASE_JSON="${REPO_ROOT}/release.json"

# ── Helper: read a scalar from release.json ───────────────────────────────────
_from_json() {
  local key="$1" default="${2:-}"
  python3 - <<PYEOF 2>/dev/null || printf '%s' "${default}"
import json, sys
try:
    d = json.load(open("${RELEASE_JSON}"))
    v = d.get("${key}")
    if v is None:
        sys.stdout.write("${default}")
    elif isinstance(v, bool):
        sys.stdout.write(str(v).lower())
    else:
        sys.stdout.write(str(v))
except Exception:
    sys.stdout.write("${default}")
PYEOF
}

# ── Load .env for local secrets (S3_BUCKET, AWS creds, …) ────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -o allexport; source "${ENV_FILE}"; set +o allexport
fi

# ── Defaults from release.json (CLI flags override below) ────────────────────
TAG="$(_from_json cell_kn_tag)"
TAR_SOURCE="$(_from_json tar_source)"
SKIP_ONTOLOGY="$(_from_json skip_ontology false)"
MAX_FETCH_AGE_HOURS="$(_from_json max_fetch_age_hours)"
RUN_NAME=""
JAVA_OPTS=""
JOB_QUEUE="${JOB_QUEUE:-nlm-ckn-release}"
JOB_DEFINITION="${JOB_DEFINITION:-nlm-ckn-release}"

# ── Argument parsing ──────────────────────────────────────────────────────────
usage() {
  grep '^#' "$0" | grep -v '#!/' | sed 's/^# \{0,1\}//'
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)                  TAG="$2";                  shift 2 ;;
    --tar-source)           TAR_SOURCE="$2";           shift 2 ;;
    --skip-ontology)        SKIP_ONTOLOGY="true";      shift   ;;
    --run-name)             RUN_NAME="$2";             shift 2 ;;
    --max-fetch-age-hours)  MAX_FETCH_AGE_HOURS="$2";  shift 2 ;;
    --java-opts)            JAVA_OPTS="$2";            shift 2 ;;
    --queue)                JOB_QUEUE="$2";            shift 2 ;;
    --job-definition)       JOB_DEFINITION="$2";       shift 2 ;;
    -h|--help)              usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -z "${TAG}" ]] && { echo "ERROR: --tag is required (or set cell_kn_tag in release.json)" >&2; usage; }

# ── Stage tarball to S3 if needed ────────────────────────────────────────────
# HTTPS URLs are downloaded here (where GITHUB_TOKEN is available) and uploaded
# to S3 so the Batch container never needs to reach GitHub directly.
# Local paths are uploaded directly.
# Existing s3:// URLs are passed through unchanged.
if [[ -n "${TAR_SOURCE}" && "${TAR_SOURCE}" != s3://* ]]; then
  : "${S3_BUCKET:?S3_BUCKET must be set to stage the release tarball}"
  S3_KEY="uploads/$(basename "${TAR_SOURCE%%\?*}")"  # strip any query string for the key

  if [[ "${TAR_SOURCE}" == http://* || "${TAR_SOURCE}" == https://* ]]; then
    echo "[trigger-release] Downloading tarball from ${TAR_SOURCE} ..."
    TMP_TAR=$(mktemp "${TMPDIR:-/tmp}/trigger-release-XXXXXX.tar.gz")
    curl_args=(--fail --location --output "${TMP_TAR}" "${TAR_SOURCE}")
    [[ -n "${GITHUB_TOKEN:-}" ]] && curl_args+=(--header "Authorization: Bearer ${GITHUB_TOKEN}")
    curl "${curl_args[@]}"
    echo "[trigger-release] Uploading → s3://${S3_BUCKET}/${S3_KEY} ..."
    aws s3 cp "${TMP_TAR}" "s3://${S3_BUCKET}/${S3_KEY}"
    rm -f "${TMP_TAR}"
  else
    echo "[trigger-release] Uploading local tarball → s3://${S3_BUCKET}/${S3_KEY} ..."
    aws s3 cp "${TAR_SOURCE}" "s3://${S3_BUCKET}/${S3_KEY}"
  fi

  TAR_SOURCE="s3://${S3_BUCKET}/${S3_KEY}"
  echo "[trigger-release] Staged: ${TAR_SOURCE}"
fi

# ── Upload release.json to S3 ─────────────────────────────────────────────────
# The Batch container reads hubmap_urls and release config from this file.
: "${S3_BUCKET:?S3_BUCKET must be set}"
RELEASE_CONFIG_S3="s3://${S3_BUCKET}/uploads/release.json"
echo "[trigger-release] Uploading release.json → ${RELEASE_CONFIG_S3} ..."
aws s3 cp "${RELEASE_JSON}" "${RELEASE_CONFIG_S3}"
echo "[trigger-release] Uploaded: ${RELEASE_CONFIG_S3}"

# ── Build container environment overrides ────────────────────────────────────
# CELL_KN_TAG and RELEASE_CONFIG are always set. The rest are only included
# when non-empty so the job definition defaults remain in effect otherwise.
env_json="[{\"name\":\"CELL_KN_TAG\",\"value\":\"${TAG}\"}"
env_json+=",{\"name\":\"RELEASE_CONFIG\",\"value\":\"${RELEASE_CONFIG_S3}\"}"
[[ -n "${TAR_SOURCE}"          ]] && env_json+=",{\"name\":\"TAR_SOURCE\",\"value\":\"${TAR_SOURCE}\"}"
[[ -n "${RUN_NAME}"            ]] && env_json+=",{\"name\":\"RUN_NAME\",\"value\":\"${RUN_NAME}\"}"
[[ -n "${MAX_FETCH_AGE_HOURS}" ]] && env_json+=",{\"name\":\"MAX_FETCH_AGE_HOURS\",\"value\":\"${MAX_FETCH_AGE_HOURS}\"}"
[[ -n "${JAVA_OPTS}"           ]] && env_json+=",{\"name\":\"JAVA_OPTS\",\"value\":\"${JAVA_OPTS}\"}"
env_json+=",{\"name\":\"SKIP_ONTOLOGY\",\"value\":\"${SKIP_ONTOLOGY}\"}"
env_json+="]"

overrides="{\"environment\":${env_json}}"

# Sanitise the tag for use as a job name (Batch allows [a-zA-Z0-9_-]).
JOB_NAME="nlm-ckn-release-${TAG//[^a-zA-Z0-9_-]/-}"

# ── Submit ────────────────────────────────────────────────────────────────────
echo "[trigger-release] Submitting: job=${JOB_NAME}  queue=${JOB_QUEUE}  definition=${JOB_DEFINITION}"

RESULT=$(aws batch submit-job \
  --job-name        "${JOB_NAME}" \
  --job-queue       "${JOB_QUEUE}" \
  --job-definition  "${JOB_DEFINITION}" \
  --container-overrides "${overrides}" \
  --output json)

JOB_ID=$(python3 -c "import sys,json; print(json.load(sys.stdin)['jobId'])" <<< "${RESULT}")

echo ""
echo "[trigger-release] Submitted successfully."
echo "  Job ID   : ${JOB_ID}"
echo "  Job name : ${JOB_NAME}"
echo ""
echo "Monitor:"
echo "  aws batch describe-jobs --jobs ${JOB_ID} --query 'jobs[0].{status:status,reason:statusReason}'"
echo "  aws logs tail /batch/nlm-ckn-release --follow"
echo "  https://console.aws.amazon.com/batch/home#jobs/detail/${JOB_ID}"
