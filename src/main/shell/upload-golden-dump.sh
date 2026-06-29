#!/usr/bin/env bash
# upload-golden-dump.sh — assume a cross-account role and upload the golden-dump
# tarball to the partner team's staging S3 bucket.
#
# The partner account (675671393318) provisioned a bucket and an IAM role that
# trusts our AWS account (952291113202). The role's policy permits s3:PutObject
# only under the DATA/ prefix, so this script assumes that role with STS and
# copies the tarball there. The uploaded object name is tagged with the ETL
# version so successive releases don't overwrite one another.
#
# Required (one of):
#   --etl-version VERSION    ETL version string, e.g. v1.4.6-rc.6
#   --version-file PATH      Read the version from a file (e.g. an ETL_VERSION)
#
# Optional:
#   --tar-source PATH        Local tarball to upload
#                            (default: data/06-golden-dump.tar.gz under repo root)
#   --dest-name NAME         Object base name in S3
#                            (default: nlm-ckn-golden-dump-${VERSION}.tar.gz)
#   --role-arn ARN           Role to assume (default: NLMDataIngressRole)
#   --bucket NAME            Destination bucket (default: partner staging bucket)
#   --prefix PREFIX          Destination key prefix (default: DATA/)
#   --region REGION          AWS region (default: ${AWS_REGION_DEFAULT})
#   --dry-run                Print the planned actions without uploading
#
# Any default above can also be overridden via the environment (GOLDEN_DUMP_*).
#
# Usage:
#   bash src/main/shell/upload-golden-dump.sh --version-file ../nlm-ckn-ui/ETL_VERSION
#   bash src/main/shell/upload-golden-dump.sh --etl-version v1.4.6-rc.6
#   bash src/main/shell/upload-golden-dump.sh --etl-version v1.4.6 --tar-source /path/to/dump.tar.gz

set -euo pipefail

EXPECTED_ACCOUNT="952291113202"   # our account, trusted by the role's trust policy

# ── Resolve repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# ── Load .env first (for AWS_PROFILE / credentials used by the base STS call) ─
# Sourced before our config defaults so its generic keys (e.g. S3_BUCKET) can't
# clobber the cross-account destination set below.
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -o allexport
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +o allexport
fi

# ── Cross-account upload target (partner account 675671393318) ───────────────
ROLE_ARN="${GOLDEN_DUMP_ROLE_ARN:-arn:aws:iam::675671393318:role/NLMDataIngressRole}"
DEST_BUCKET="${GOLDEN_DUMP_BUCKET:-nlm-2026-staging-graphs-ec2-675671393318}"
DEST_PREFIX="${GOLDEN_DUMP_PREFIX:-DATA/}"
REGION="${GOLDEN_DUMP_REGION:-eu-north-1}"

# ── Defaults ──────────────────────────────────────────────────────────────────
ETL_VERSION=""
VERSION_FILE=""
TAR_SOURCE="${TAR_SOURCE:-${REPO_ROOT}/data/06-golden-dump.tar.gz}"
DEST_NAME=""
DRY_RUN=0

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
    --etl-version)  _require_arg "$1" "${2-}"; ETL_VERSION="${2-}";  shift 2 ;;
    --version-file) _require_arg "$1" "${2-}"; VERSION_FILE="${2-}"; shift 2 ;;
    --tar-source)   _require_arg "$1" "${2-}"; TAR_SOURCE="${2-}";   shift 2 ;;
    --dest-name)    _require_arg "$1" "${2-}"; DEST_NAME="${2-}";    shift 2 ;;
    --role-arn)     _require_arg "$1" "${2-}"; ROLE_ARN="${2-}";     shift 2 ;;
    --bucket)       _require_arg "$1" "${2-}"; DEST_BUCKET="${2-}";  shift 2 ;;
    --prefix)       _require_arg "$1" "${2-}"; DEST_PREFIX="${2-}";  shift 2 ;;
    --region)       _require_arg "$1" "${2-}"; REGION="${2-}";       shift 2 ;;
    --dry-run)      DRY_RUN=1;                                       shift   ;;
    -h|--help)      usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

# ── Resolve the ETL version ───────────────────────────────────────────────────
if [[ -n "${VERSION_FILE}" ]]; then
  [[ -f "${VERSION_FILE}" ]] || { echo "ERROR: version file not found: ${VERSION_FILE}" >&2; exit 1; }
  ETL_VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"
fi

[[ -z "${ETL_VERSION}" ]] && {
  echo "ERROR: provide --etl-version VERSION or --version-file PATH" >&2; usage
}

# ── Validate the source tarball ───────────────────────────────────────────────
[[ -f "${TAR_SOURCE}" ]] || { echo "ERROR: tar source not found: ${TAR_SOURCE}" >&2; exit 1; }

# ── Compose the destination key (prefix is always DATA/) ─────────────────────
DEST_PREFIX="${DEST_PREFIX#/}"                                # no leading slash
DEST_PREFIX="${DEST_PREFIX%/}/"                               # exactly one trailing slash
# The ingress role policy only permits s3:PutObject under DATA/; reject anything
# that would escape that prefix before we ever attempt the upload.
if [[ "${DEST_PREFIX}" != "DATA/" && "${DEST_PREFIX}" != DATA/* ]]; then
  echo "ERROR: destination prefix must be under DATA/ (got: ${DEST_PREFIX})" >&2
  exit 1
fi
DEST_NAME="${DEST_NAME:-nlm-ckn-golden-dump-${ETL_VERSION}.tar.gz}"
S3_URI="s3://${DEST_BUCKET}/${DEST_PREFIX}${DEST_NAME}"

echo "[upload-golden-dump] ETL version : ${ETL_VERSION}"
echo "[upload-golden-dump] Source       : ${TAR_SOURCE} ($(du -h "${TAR_SOURCE}" | cut -f1))"
echo "[upload-golden-dump] Destination  : ${S3_URI}"
echo "[upload-golden-dump] Role         : ${ROLE_ARN}"
echo "[upload-golden-dump] Region       : ${REGION}"

# ── Confirm we are operating from the trusted account before assuming ─────────
CALLER_ACCOUNT="$(aws sts get-caller-identity --query 'Account' --output text 2>/dev/null || true)"
if [[ -z "${CALLER_ACCOUNT}" ]]; then
  echo "ERROR: unable to read caller identity — are AWS credentials configured?" >&2
  exit 1
fi
if [[ "${CALLER_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]]; then
  echo "WARNING: current account ${CALLER_ACCOUNT} is not the trusted account ${EXPECTED_ACCOUNT};" >&2
  echo "         the assume-role call will fail unless this account is also trusted." >&2
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[upload-golden-dump] --dry-run set; not assuming role or uploading."
  exit 0
fi

# ── Assume the cross-account ingress role ────────────────────────────────────
SESSION_NAME="nlm-ckn-golden-dump-$(date +%Y%m%dT%H%M%SZ)"
echo "[upload-golden-dump] Assuming role (session: ${SESSION_NAME}) ..."

read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN < <(
  aws sts assume-role \
    --role-arn "${ROLE_ARN}" \
    --role-session-name "${SESSION_NAME}" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
)

if [[ -z "${AWS_ACCESS_KEY_ID:-}" || "${AWS_ACCESS_KEY_ID}" == "None" ]]; then
  echo "ERROR: failed to assume role ${ROLE_ARN}" >&2
  exit 1
fi
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# ── Upload under the DATA/ prefix (the only prefix the role policy permits) ──
echo "[upload-golden-dump] Uploading → ${S3_URI} ..."
aws s3 cp "${TAR_SOURCE}" "${S3_URI}" --region "${REGION}"

echo ""
echo "[upload-golden-dump] Upload complete."
echo "  Object: ${S3_URI}"
