#!/usr/bin/env bash
# trigger-release.sh — submit an NLM-CKN release job to AWS Batch and exit.
#
# The job runs release.py on EC2 (fetch + full ETL). This script returns
# immediately after submission; monitor progress via CloudWatch or the
# AWS Batch console.
#
# Config file (gitignored):
#   .env  — loaded automatically if present
#
# .env format (KEY=value, no quotes required):
#   S3_BUCKET=my-bucket
#
# Required:
#   --tag TAG        cell-kn release tag, e.g. v2026-04
#
# Optional:
#   --tar-source PATH_OR_URL
#       Local path, S3 URL (s3://...), or HTTPS URL for the release tarball.
#       If a local path is given the file is uploaded to S3 before submission.
#       Default: derived from --tag (GitHub Releases asset URL).
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
#   bash src/main/shell/trigger-release.sh --tag v2026-04
#   bash src/main/shell/trigger-release.sh --tag v2026-04 --tar-source /path/to/prod-data-v2026-04.tar.gz
#   bash src/main/shell/trigger-release.sh --tag v2026-04 --skip-ontology

set -euo pipefail

# ── Resolve repo root and load .env ──────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -o allexport; source "${ENV_FILE}"; set +o allexport
fi

# ── Defaults ──────────────────────────────────────────────────────────────────
TAG=""
TAR_SOURCE=""
SKIP_ONTOLOGY="false"
RUN_NAME=""
MAX_FETCH_AGE_HOURS=""
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

[[ -z "${TAG}" ]] && { echo "ERROR: --tag is required" >&2; usage; }

# ── Upload local tarball to S3 if needed ─────────────────────────────────────
if [[ -n "${TAR_SOURCE}" && -f "${TAR_SOURCE}" ]]; then
  : "${S3_BUCKET:?S3_BUCKET must be set to upload a local tarball}"
  S3_KEY="uploads/$(basename "${TAR_SOURCE}")"
  echo "[trigger-release] Uploading local tarball → s3://${S3_BUCKET}/${S3_KEY} ..."
  aws s3 cp "${TAR_SOURCE}" "s3://${S3_BUCKET}/${S3_KEY}"
  TAR_SOURCE="s3://${S3_BUCKET}/${S3_KEY}"
  echo "[trigger-release] Uploaded: ${TAR_SOURCE}"
fi

# ── Upload hubmap_urls.txt to S3 ──────────────────────────────────────────────
# The file is not bundled in the release tarball and must be available to the
# batch container. Upload it from the local repo so release.py can fetch it.
HUBMAP_URLS_FILE="${HUBMAP_URLS_FILE:-${REPO_ROOT}/data/hubmap_urls.txt}"
if [[ ! -f "${HUBMAP_URLS_FILE}" ]]; then
  echo "ERROR: hubmap_urls.txt not found at ${HUBMAP_URLS_FILE}" >&2
  echo "Set HUBMAP_URLS_FILE to the correct path." >&2
  exit 1
fi
: "${S3_BUCKET:?S3_BUCKET must be set to upload hubmap_urls.txt}"
HUBMAP_S3_KEY="uploads/hubmap_urls.txt"
echo "[trigger-release] Uploading hubmap_urls.txt → s3://${S3_BUCKET}/${HUBMAP_S3_KEY} ..."
aws s3 cp "${HUBMAP_URLS_FILE}" "s3://${S3_BUCKET}/${HUBMAP_S3_KEY}"
HUBMAP_URLS_FILE="s3://${S3_BUCKET}/${HUBMAP_S3_KEY}"
echo "[trigger-release] Uploaded: ${HUBMAP_URLS_FILE}"

# ── Build container environment overrides ────────────────────────────────────
# CELL_KN_TAG is always set. The rest are only included when non-empty so the
# job definition defaults (and release.py defaults) remain in effect otherwise.
env_json="[{\"name\":\"CELL_KN_TAG\",\"value\":\"${TAG}\"}"
[[ -n "${TAR_SOURCE}"          ]] && env_json+=",{\"name\":\"TAR_SOURCE\",\"value\":\"${TAR_SOURCE}\"}"
[[ -n "${HUBMAP_URLS_FILE}"    ]] && env_json+=",{\"name\":\"HUBMAP_URLS_FILE\",\"value\":\"${HUBMAP_URLS_FILE}\"}"
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
