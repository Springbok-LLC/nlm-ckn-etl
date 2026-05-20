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
#   PIPELINE_IMAGE (env)
#       Full ECR image URI to use for the Batch container (e.g. 123.dkr.ecr…/pipeline:v1.0).
#       Set automatically by on-release.yml from the build job output.
#       When unset the Batch job definition's default image (:latest) is used.
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

_require_arg() {
  if [[ -z "${2:-}" || "${2}" == -* ]]; then
    echo "ERROR: $1 requires a value" >&2; usage
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)                  _require_arg "$1" "${2-}"; TAG="${2-}";                  shift 2 ;;
    --tar-source)           _require_arg "$1" "${2-}"; TAR_SOURCE="${2-}";           shift 2 ;;
    --skip-ontology)        SKIP_ONTOLOGY="true";                                    shift   ;;
    --run-name)             _require_arg "$1" "${2-}"; RUN_NAME="${2-}";             shift 2 ;;
    --max-fetch-age-hours)  _require_arg "$1" "${2-}"; MAX_FETCH_AGE_HOURS="${2-}";  shift 2 ;;
    --java-opts)            _require_arg "$1" "${2-}"; JAVA_OPTS="${2-}";            shift 2 ;;
    --queue)                _require_arg "$1" "${2-}"; JOB_QUEUE="${2-}";            shift 2 ;;
    --job-definition)       _require_arg "$1" "${2-}"; JOB_DEFINITION="${2-}";       shift 2 ;;
    -h|--help)              usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -z "${TAG}" ]] && { echo "ERROR: --tag is required (or set cell_kn_tag in release.json)" >&2; usage; }

# Derive the run name the same way release.py does: strip a leading 'v' from
# the tag, unless --run-name was given explicitly.
RUN="${RUN_NAME:-${TAG#v}}"

# ── Stage tarball to S3 if needed ────────────────────────────────────────────
# HTTPS URLs are downloaded here (where GITHUB_TOKEN is available) and uploaded
# to S3 so the Batch container never needs to reach GitHub directly.
# Local paths are uploaded directly.
# Existing s3:// URLs are passed through unchanged.
# Files go under runs/{run}/ so concurrent releases don't share a staging area.
if [[ -n "${TAR_SOURCE}" && "${TAR_SOURCE}" != s3://* ]]; then
  : "${S3_BUCKET:?S3_BUCKET must be set to stage the release tarball}"
  S3_KEY="runs/${RUN}/$(basename "${TAR_SOURCE%%\?*}")"  # strip any query string for the key

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
# Scoped to runs/{run}/ so concurrent releases don't overwrite each other's config.
: "${S3_BUCKET:?S3_BUCKET must be set}"
RELEASE_CONFIG_S3="s3://${S3_BUCKET}/runs/${RUN}/release.json"
echo "[trigger-release] Uploading release.json → ${RELEASE_CONFIG_S3} ..."
aws s3 cp "${RELEASE_JSON}" "${RELEASE_CONFIG_S3}"
echo "[trigger-release] Uploaded: ${RELEASE_CONFIG_S3}"

# ── Create GitHub deployment ──────────────────────────────────────────────────
# If GITHUB_TOKEN and GITHUB_REPOSITORY are set, create a deployment for the
# tag and post an in_progress status.  The Batch container uses GITHUB_DEPLOYMENT_ID
# to post the final success/failure status when the pipeline completes.
GITHUB_DEPLOYMENT_ID=""
if [[ -n "${GITHUB_DEPLOY_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_REF_NAME:-}" ]]; then
  # Mark any previous in_progress deployments for this environment as inactive
  # so the deployments page doesn't accumulate stale "Deploying" entries.
  DEPLOYMENTS_JSON=$(curl -s \
    -H "Authorization: Bearer ${GITHUB_DEPLOY_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/deployments?environment=production&per_page=20") || true
  _INPUT=$(mktemp /tmp/trigger-input-XXXXXXXX.json)
  printf '%s' "${DEPLOYMENTS_JSON}" > "${_INPUT}"
  STALE_IDS=$(
    GITHUB_DEPLOY_TOKEN="${GITHUB_DEPLOY_TOKEN}" \
    GITHUB_REF_NAME="${GITHUB_REF_NAME:-}" GITHUB_ACTOR="${GITHUB_ACTOR:-}" \
    python3 - "${_INPUT}" 2>/dev/null <<'PYEOF'
import json, os, sys, urllib.request
token = os.environ["GITHUB_DEPLOY_TOKEN"]
deploys = json.load(open(sys.argv[1]))
ref    = os.environ.get("GITHUB_REF_NAME", "")
actor  = os.environ.get("GITHUB_ACTOR", "")
for d in deploys:
    if ref and d.get("ref") != ref:
        continue
    if actor and d.get("creator", {}).get("login") != actor:
        continue
    req = urllib.request.Request(d['statuses_url'],
        headers={'Authorization': f'Bearer {token}',
                 'Accept': 'application/vnd.github+json'})
    try:
        statuses = json.loads(urllib.request.urlopen(req, timeout=5).read())
        latest = statuses[0]['state'] if statuses else 'pending'
    except Exception:
        latest = 'unknown'
    if latest in ('in_progress', 'pending'):
        print(d['id'])
PYEOF
  ) || true
  rm -f "${_INPUT}"
  for stale_id in ${STALE_IDS}; do
    curl -sf -X POST \
      -H "Authorization: Bearer ${GITHUB_DEPLOY_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${GITHUB_REPOSITORY}/deployments/${stale_id}/statuses" \
      -d '{"state":"inactive"}' > /dev/null 2>&1 || true
  done

  echo "[trigger-release] Creating GitHub deployment for ${GITHUB_REF_NAME} (nlm-ckn tag: ${TAG}) ..."
  DEPLOY_RESPONSE=$(curl -s -X POST \
    -H "Authorization: Bearer ${GITHUB_DEPLOY_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/deployments" \
    -d "{\"ref\":\"${GITHUB_REF_NAME}\",\"environment\":\"production\",\"description\":\"nlm-ckn ${TAG}\",\"auto_merge\":false,\"required_contexts\":[]}")
  GITHUB_DEPLOYMENT_ID=$(python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('id',''))" \
    <<< "${DEPLOY_RESPONSE}" 2>/dev/null) || true
  if [[ -n "${GITHUB_DEPLOYMENT_ID}" ]]; then
    echo "[trigger-release] Deployment ID: ${GITHUB_DEPLOYMENT_ID}"
    curl -sf -X POST \
      -H "Authorization: Bearer ${GITHUB_DEPLOY_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${GITHUB_REPOSITORY}/deployments/${GITHUB_DEPLOYMENT_ID}/statuses" \
      -d '{"state":"in_progress","description":"Batch job submitted"}' \
      > /dev/null 2>&1 || true
  else
    echo "[trigger-release] Warning: could not create GitHub deployment (check GITHUB_TOKEN scope)" >&2
  fi
fi

# ── Build container environment overrides ────────────────────────────────────
# CELL_KN_TAG and RELEASE_CONFIG are always set. The rest are only included
# when non-empty so the job definition defaults remain in effect otherwise.
# Use Python json.dumps for safe JSON building to handle special characters.
# GITHUB_TOKEN is intentionally omitted here — it is injected by the Batch
# job definition via Secrets Manager (see cloudformation/batch.yaml).
env_json=$(
  CELL_KN_TAG="${TAG}" \
  RELEASE_CONFIG_S3="${RELEASE_CONFIG_S3}" \
  TAR_SOURCE="${TAR_SOURCE}" \
  RUN_NAME="${RUN_NAME}" \
  MAX_FETCH_AGE_HOURS="${MAX_FETCH_AGE_HOURS}" \
  JAVA_OPTS="${JAVA_OPTS}" \
  GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}" \
  GITHUB_DEPLOYMENT_ID="${GITHUB_DEPLOYMENT_ID}" \
  SKIP_ONTOLOGY="${SKIP_ONTOLOGY}" \
  python3 - <<'PYEOF'
import json, os
env = [
    {"name": "CELL_KN_TAG",      "value": os.environ["CELL_KN_TAG"]},
    {"name": "RELEASE_CONFIG",   "value": os.environ["RELEASE_CONFIG_S3"]},
    {"name": "SKIP_ONTOLOGY",    "value": os.environ["SKIP_ONTOLOGY"]},
]
for key, envvar in [
    ("TAR_SOURCE",          "TAR_SOURCE"),
    ("RUN_NAME",            "RUN_NAME"),
    ("MAX_FETCH_AGE_HOURS", "MAX_FETCH_AGE_HOURS"),
    ("JAVA_OPTS",           "JAVA_OPTS"),
    ("GITHUB_REPOSITORY",   "GITHUB_REPOSITORY"),
    ("GITHUB_DEPLOYMENT_ID","GITHUB_DEPLOYMENT_ID"),
]:
    val = os.environ.get(envvar, "")
    if val:
        env.append({"name": key, "value": val})
print(json.dumps(env))
PYEOF
)

# Sanitise the tag for use as a job name (Batch allows [a-zA-Z0-9_-]).
LABEL="${RUN_NAME:-${TAG}}"
JOB_NAME="nlm-ckn-release-${LABEL//[^a-zA-Z0-9_-]/-}"

overrides="{\"environment\":${env_json}}"

# If a specific pipeline image was supplied (set by the build job in CI),
# register a new job-definition revision that pins that image, then submit
# against that revision.  AWS Batch containerOverrides does not accept
# "image" as a field — the image can only be changed via a new revision.
SUBMIT_JOB_DEFINITION="${JOB_DEFINITION}"
if [[ -n "${PIPELINE_IMAGE:-}" ]]; then
  echo "[trigger-release] Using pipeline image: ${PIPELINE_IMAGE}"

  # Fetch the existing job definition's container properties and swap the image.
  EXISTING=$(aws batch describe-job-definitions \
    --job-definition-name "${JOB_DEFINITION}" \
    --status ACTIVE \
    --query 'jobDefinitions[0]' \
    --output json)

  _INPUT=$(mktemp /tmp/trigger-input-XXXXXXXX.json)
  printf '%s' "${EXISTING}" > "${_INPUT}"
  REGISTER_JSON=$(
    PIPELINE_IMAGE="${PIPELINE_IMAGE}" JOB_DEFINITION="${JOB_DEFINITION}" \
    python3 - "${_INPUT}" <<'PYEOF'
import json, os, sys
jd = json.load(open(sys.argv[1]))
cp = jd["containerProperties"]
cp["image"] = os.environ["PIPELINE_IMAGE"]
for key in ("taskArn",):
    cp.pop(key, None)
out = {
    "jobDefinitionName": os.environ["JOB_DEFINITION"],
    "type": "container",
    "containerProperties": cp,
}
# tags/propagateTags excluded — require batch:TagResource, should not be copied
for field in ("retryStrategy", "timeout", "platformCapabilities",
              "schedulingPriority", "parameters"):
    if field in jd:
        out[field] = jd[field]
print(json.dumps(out))
PYEOF
  )
  rm -f "${_INPUT}"

  NEW_DEF=$(aws batch register-job-definition \
    --cli-input-json "${REGISTER_JSON}" \
    --query 'jobDefinitionArn' \
    --output text)

  echo "[trigger-release] Registered new job definition revision: ${NEW_DEF}"
  SUBMIT_JOB_DEFINITION="${NEW_DEF}"
fi

# ── Submit ────────────────────────────────────────────────────────────────────
echo "[trigger-release] Submitting: job=${JOB_NAME}  queue=${JOB_QUEUE}  definition=${SUBMIT_JOB_DEFINITION}"

RESULT=$(aws batch submit-job \
  --job-name        "${JOB_NAME}" \
  --job-queue       "${JOB_QUEUE}" \
  --job-definition  "${SUBMIT_JOB_DEFINITION}" \
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
