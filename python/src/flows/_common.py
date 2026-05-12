"""Shared constants, helpers, and Prefect tasks for the NLM-CKN ETL.

Imported by both ``flows/fetch.py`` (external API data collection) and
``flows/pipeline.py`` (data processing and graph building) to avoid duplication.

Design note
-----------
All Python and Java scripts are invoked **directly** via ``subprocess`` using
the same interpreter / JRE that is already installed on the host (EC2 or
ECS Fargate task).  There are no Docker-in-Docker calls here.

- Python scripts run with ``sys.executable`` so they share the host's
  installed packages (cellxgene-census, scanpy, etc.).
- Java programs run with the ``java`` binary on ``PATH``; the JAR is either
  downloaded from S3 or built locally by CI/CD.
- ``PYTHONPATH`` is always set to ``python/src/`` so scripts can import
  sibling modules (``LoaderUtilities``, ``ArangoDbUtilities``, etc.).
"""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import boto3

import docker as docker_sdk
from prefect import get_run_logger, task

# ── Constants ──────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parents[3]

# Relative path to the compiled JAR (from REPO_ROOT).
# The JAR is downloaded from S3 by ``ensure_jar()`` in flows/pipeline.py, or
# built locally with ``mvn clean package -DskipTests`` for development.
CLASSPATH = "target/nlm-ckn-etl-1.0.jar"

# Default Java heap.  Raise with --java-opts if OOM-killed (exit 137).
DEFAULT_JAVA_OPTS = "-Xmx32g"

ARANGO_DB_HOST = os.getenv("ARANGO_DB_HOST", "localhost")
ARANGO_DB_PORT = int(os.getenv("ARANGO_DB_PORT", "8529"))
ARANGO_DB_HOME = os.getenv("ARANGO_DB_HOME", str(REPO_ROOT / "data" / "arangodb"))

# Host-side path for the ArangoDB data directory, used as the Docker volume
# source when starting the ArangoDB sibling container via the Docker socket.
#
# When the pipeline itself runs inside Docker (with /var/run/docker.sock
# mounted), volume paths passed to the Docker SDK are resolved by the HOST
# daemon.  ARANGO_DB_HOME is a container-internal path and therefore unknown
# to the host daemon — this causes a "path not shared from host" error.
#
# Two ways to resolve this:
#   1. Set ARANGO_DB_HOST_HOME to the host-side path that corresponds to
#      ARANGO_DB_HOME, e.g.:
#        -e ARANGO_DB_HOST_HOME=$(pwd)/data/arangodb
#      start_arangodb will bind-mount that path into the ArangoDB container.
#   2. Leave ARANGO_DB_HOST_HOME unset.  When running inside a container
#      (detected by /.dockerenv), start_arangodb falls back to the named
#      Docker volume "nlm-ckn-arangodb-data", which the host daemon manages
#      without needing a host path.
ARANGO_DB_HOST_HOME = os.getenv("ARANGO_DB_HOST_HOME", "")

# Named Docker volume used as the ArangoDB data volume when running inside a
# container without ARANGO_DB_HOST_HOME set.  The volume is created on first
# use and persists across pipeline runs.
ARANGO_DB_VOLUME_NAME = "nlm-ckn-arangodb-data"

# S3 bucket for durable storage of external cache, tuples, JAR, and archives.
# Empty string → local-only mode (no S3 operations performed).
S3_BUCKET = os.getenv("S3_BUCKET", "")

# PYTHONPATH injected into every direct Python script invocation so that
# sibling imports (LoaderUtilities, ArangoDbUtilities, …) resolve correctly.
PYTHON_SRC = str(REPO_ROOT / "python" / "src")


# ── Private helpers ────────────────────────────────────────────────────────


def _get_or_create_arango_password() -> str:
    """Read the ArangoDB root password from .arangodb-password, creating it on first run."""
    password_file = REPO_ROOT / ".arangodb-password"
    if password_file.exists():
        return password_file.read_text().strip()
    password = secrets.token_urlsafe(24)
    password_file.write_text(password)
    return password


def _find_free_port() -> int:
    """Return an OS-assigned free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _get_arangodb_id() -> str | None:
    """Return the short container ID of a running ArangoDB container, or None.

    Uses the Docker SDK (no ``docker`` CLI binary required).  Returns ``None``
    if Docker is unreachable or no ArangoDB container is running.

    Checks in order:
    1. A container named exactly ``arangodb`` (the name we assign on start).
    2. Any running container built from the ``arangodb`` image (ancestor filter),
       as a fallback for containers started outside this script.
    """
    try:
        client = docker_sdk.from_env()
        # Primary: by name (fast, exact)
        named = client.containers.list(
            filters={"name": "arangodb", "status": "running"}
        )
        if named:
            return named[0].short_id
        # Fallback: by image ancestor (catches containers with random names)
        by_image = client.containers.list(
            filters={"ancestor": "arangodb", "status": "running"}
        )
        return by_image[0].short_id if by_image else None
    except docker_sdk.errors.DockerException:
        return None


def _arango_env(arango_db_password: str) -> dict[str, str]:
    """Return environment variables for ArangoDB connectivity.

    Injected into every Python script and Java program subprocess so they
    can reach the ArangoDB instance regardless of where it runs.
    """
    return {
        "ARANGO_DB_HOST": ARANGO_DB_HOST,
        "ARANGO_DB_PORT": str(ARANGO_DB_PORT),
        "ARANGO_DB_USER": "root",
        "ARANGO_DB_PASSWORD": arango_db_password,
    }


def _run_python_script(
    script: str,
    arango_db_password: str,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> None:
    """Run a Python script directly using ``sys.executable``.

    The script runs in the same interpreter (and therefore the same installed
    packages) as the Prefect worker.  ``PYTHONPATH`` is set to ``python/src/``
    so scripts can ``import LoaderUtilities``, ``import ArangoDbUtilities``,
    etc. without modification.

    Parameters
    ----------
    script:
        Filename relative to ``python/src/`` (e.g. ``"DataFetcher.py"``).
    arango_db_password:
        ArangoDB root password, forwarded as ``ARANGO_DB_PASSWORD``.
    extra_env:
        Additional environment variables to merge in (e.g. NCBI credentials).
    extra_args:
        Additional command-line arguments appended to the script invocation
        (e.g. ``["--force-all"]``).
    """
    env = {
        **os.environ,
        "PYTHONPATH": PYTHON_SRC,
        "PYTHONUNBUFFERED": "1",
        **_arango_env(arango_db_password),
        **(extra_env or {}),
    }
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "python" / "src" / script),
            *(extra_args or []),
        ],
        check=True,
        env=env,
    )


def _parse_s3_url(s3_url: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``."""
    without_scheme = s3_url[len("s3://") :]
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


def _s3_upload_tar(local_dir: Path, s3_path: str) -> None:
    """Compress ``local_dir`` to a .tar.gz and upload to ``s3_path``.

    Produces a single object with a stable hash, reducing per-file S3 API
    overhead and enabling integrity checking.  No-op when ``S3_BUCKET`` is empty.
    """
    if not S3_BUCKET:
        return
    local_dir = Path(local_dir)
    bucket, key = _parse_s3_url(s3_path)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(local_dir, arcname=local_dir.name,
                    filter=lambda m: None if "/.archive/" in m.name else m)
        boto3.client("s3").upload_file(str(tmp_path), bucket, key)
    finally:
        tmp_path.unlink(missing_ok=True)


def _s3_download_tar(s3_path: str, local_dir: Path) -> None:
    """Download a .tar.gz from ``s3_path`` and extract its contents into ``local_dir``.

    The top-level directory inside the archive is stripped so that files land
    directly in ``local_dir`` (mirrors the extraction pattern used in
    ``dump_arangodb``).  No-op when ``S3_BUCKET`` is empty.
    """
    if not S3_BUCKET:
        return
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    bucket, key = _parse_s3_url(s3_path)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        boto3.client("s3").download_file(bucket, key, str(tmp_path))
        with tarfile.open(tmp_path, "r:gz") as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts
                if len(parts) <= 1:
                    continue
                member.name = str(Path(*parts[1:]))
                tar.extract(member, local_dir)
    finally:
        tmp_path.unlink(missing_ok=True)


def _s3_copy_prefix(bucket: str, src_prefix: str, dst_prefix: str) -> int:
    """Server-side copy all objects under ``src_prefix`` to ``dst_prefix``.

    Uses S3 ``CopyObject`` so no data travels through the client.  Both
    prefixes must be in the same bucket.  Returns the number of objects copied.
    """
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=src_prefix):
        for obj in page.get("Contents", []):
            src_key = obj["Key"]
            relative = src_key[len(src_prefix) :]
            s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": src_key},
                Key=dst_prefix + relative,
            )
            count += 1
    return count


def _s3_sync(src: str, dst: str) -> None:
    """Sync a directory between local filesystem and S3, skipping unchanged files.

    Detects direction from whether ``src`` or ``dst`` starts with ``s3://``.
    Unchanged files are identified by size, matching ``aws s3 sync`` behaviour.
    No-op when ``S3_BUCKET`` is empty (local-only mode).
    """
    if not S3_BUCKET:
        return
    s3 = boto3.client("s3")
    if src.startswith("s3://"):
        # Download: S3 → local
        bucket, prefix = _parse_s3_url(src)
        local_dir = Path(dst)
        local_dir.mkdir(parents=True, exist_ok=True)
        local_index = {
            p.relative_to(local_dir).as_posix(): p.stat().st_size
            for p in local_dir.rglob("*")
            if p.is_file()
        }
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                relative = obj["Key"][len(prefix) :]
                if not relative:
                    continue
                if relative not in local_index or local_index[relative] != obj["Size"]:
                    local_path = local_dir / relative
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    s3.download_file(bucket, obj["Key"], str(local_path))
    else:
        # Upload: local → S3
        local_dir = Path(src)
        bucket, prefix = _parse_s3_url(dst)
        s3_index: dict[str, int] = {}
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                relative = obj["Key"][len(prefix) :]
                if relative:
                    s3_index[relative] = obj["Size"]
        for path in local_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(local_dir).as_posix()
            if relative not in s3_index or s3_index[relative] != path.stat().st_size:
                s3.upload_file(str(path), bucket, prefix + relative)


def _s3_cp(src: str, dst: str) -> None:
    """Upload a single local file to S3.

    No-op when ``S3_BUCKET`` is empty (local-only mode).
    """
    if not S3_BUCKET:
        return
    bucket, key = _parse_s3_url(dst)
    boto3.client("s3").upload_file(src, bucket, key)


# ── Shared tasks ───────────────────────────────────────────────────────────


def _jar_key() -> str:
    """Return a 16-char SHA-256 prefix of the compiled JAR, used to key baseline dumps in S3.

    Content-addressed so the key changes whenever the JAR changes, regardless
    of whether the pom.xml version string was bumped.  Call only after
    ``ensure_jar`` has confirmed the JAR is present.
    """
    jar = REPO_ROOT / CLASSPATH
    if not jar.exists():
        raise FileNotFoundError(f"JAR not found at {jar} — run ensure_jar first")
    h = hashlib.sha256()
    with jar.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _external_dir(run: str = "") -> Path:
    """Return the run-specific external cache directory.

    Mirrors ``RunConfig.external_dir`` (``data/external-{run_name}``) without
    importing ``LoaderUtilities`` (which pulls in heavy scientific packages).
    """
    import os as _os

    run_name = run or _os.getenv("CKN_RUN", "full")
    return REPO_ROOT / "data" / f"external-{run_name}"


@task(name="clean-empty-external-files", log_prints=True)
def clean_empty_external_files(run: str = "") -> None:
    """Remove corrupt or structurally invalid files from ``data/external-<run>/``.

    ``DataFetcher.py`` uses cache files in ``data/external-<run>/``
    to resume interrupted runs.  Two classes of bad files are cleaned here:

    1. **Zero-byte files** — causes ``JSONDecodeError`` on next load.

    2. **Structurally invalid cache files** — the fetcher writes a sentinel
       key into each cache file so the resume branch can reconstruct its
       working state.  A file without its sentinel raises ``KeyError``.

       Known sentinels:
       - ``gene.json``    → ``"gene_entrez_ids"``
       - ``uniprot.json`` → ``"protein_accessions"``

    Parameters
    ----------
    run:
        Run name (selects ``data/external-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    external_dir = _external_dir(run)
    external_dir.mkdir(parents=True, exist_ok=True)

    # 1. Remove zero-byte files
    removed = [
        f for f in external_dir.iterdir() if f.is_file() and f.stat().st_size == 0
    ]
    if removed:
        for f in removed:
            f.unlink()
            logger.warning(f"Removed empty/corrupt external cache file: {f.name}")
        logger.info(f"Cleaned {len(removed)} empty file(s) from {external_dir.name}/")
    else:
        logger.info(f"No empty files found in {external_dir.name}/")

    # 2. Remove cache files missing their sentinel key
    sentinel_keys = {
        "gene.json": "gene_entrez_ids",
        "uniprot.json": "protein_accessions",
    }
    for filename, key in sentinel_keys.items():
        path = external_dir / filename
        if path.exists() and path.stat().st_size > 0:
            try:
                data = json.loads(path.read_text())
                if key not in data:
                    path.unlink()
                    logger.warning(
                        f"Removed {external_dir.name}/{filename}: "
                        f"missing sentinel key '{key}' (would cause KeyError)"
                    )
            except json.JSONDecodeError:
                pass  # already handled by the zero-byte check above


@task(name="validate-external-files", log_prints=True)
def validate_external_files(run: str = "") -> None:
    """Verify that all required external cache files exist and contain valid JSON.

    Called by the fetch flow after fetching+transforming and by the pipeline
    flow after syncing from S3, ensuring TupleWriters never run against missing
    or corrupt inputs.

    Raw files checked: ``cellxgene.json``, ``opentargets.json``, ``gene.json``,
    ``uniprot.json``.  Transformed files checked: ``cellxgene_transformed.json``,
    ``opentargets_transformed.json``, ``gene_transformed.json``,
    ``uniprot_transformed.json``.

    Parameters
    ----------
    run:
        Run name (selects ``data/external-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    external_dir = _external_dir(run)
    raw_required = [
        "cellxgene.json",
        "opentargets.json",
        "gene.json",
        "uniprot.json",
        "pubmed.json",
    ]
    transformed_required = [
        "cellxgene_transformed.json",
        "opentargets_transformed.json",
        "gene_transformed.json",
        "uniprot_transformed.json",
    ]

    # pubmed.json is legitimately empty when no author-to-CL mapping files exist
    may_be_empty = {"pubmed.json"}

    errors = []
    for filename in raw_required + transformed_required:
        path = external_dir / filename
        if not path.exists():
            errors.append(f"  {filename} — file not found")
        elif path.stat().st_size == 0:
            errors.append(f"  {filename} — empty (zero bytes)")
        else:
            try:
                data = json.loads(path.read_text())
                if not data and filename not in may_be_empty:
                    logger.warning(
                        f"{external_dir.name}/{filename} is valid JSON but contains no entries "
                        f"— annotations from this source will be skipped. "
                        f"Run flows/fetch.py to populate it."
                    )
                else:
                    logger.info(
                        f"OK: {external_dir.name}/{filename} ({path.stat().st_size:,} bytes)"
                    )
            except json.JSONDecodeError as exc:
                errors.append(f"  {filename} — invalid JSON: {exc}")

    if errors:
        raise RuntimeError(
            f"Required external cache files are missing or invalid in {external_dir.name}/.\n"
            "Run flows/fetch.py first (or set S3_BUCKET so the pipeline can sync them):\n"
            + "\n".join(errors)
        )


@task(name="sync-external-from-s3", log_prints=True)
def sync_external_from_s3(run: str = "") -> None:
    """Restore the external API cache from S3 to ``data/external-<run>/``.

    No-op when ``S3_BUCKET`` is empty (local-only mode).  Used by both the
    fetch flow (to resume an interrupted run) and the pipeline flow (to pull
    the cache that the fetch flow produced).

    Parameters
    ----------
    run:
        Run name (selects ``data/external-<run>/`` and S3 prefix
        ``external-<run>/``).  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping S3 sync (local mode)")
        return
    external_dir = _external_dir(run)
    external_dir.mkdir(parents=True, exist_ok=True)
    s3_prefix = f"s3://{S3_BUCKET}/external/"
    logger.info(f"Syncing {s3_prefix} → {external_dir.name}/")
    _s3_sync(s3_prefix, str(external_dir))
    logger.info("External cache restored from S3")


@task(name="sync-external-to-s3", log_prints=True)
def sync_external_to_s3(run: str = "") -> None:
    """Push the external API cache from ``data/external-<run>/`` to S3.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run:
        Run name (selects ``data/external-<run>/`` and S3 prefix
        ``external-<run>/``).  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping S3 sync (local mode)")
        return
    external_dir = _external_dir(run)
    s3_prefix = f"s3://{S3_BUCKET}/external/"
    logger.info(f"Syncing {external_dir.name}/ → {s3_prefix}")
    _s3_sync(str(external_dir), s3_prefix)
    logger.info("External cache pushed to S3")


@task(name="sync-external-to-s3-staging", log_prints=True)
def sync_external_to_s3_staging(run: str = "") -> None:
    """Push the external API cache to the staging prefix ``external-staging/``.

    Writes to ``s3://{S3_BUCKET}/external-staging/`` rather than the live
    ``external/`` prefix so that a concurrent ``pipeline.py`` reading from
    ``external/`` sees only complete, validated snapshots.  Call
    ``promote_external_staging`` after validation to atomically swap the
    staging data into the live prefix.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run:
        Run name (selects ``data/external-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping S3 sync (local mode)")
        return
    external_dir = _external_dir(run)
    s3_staging = f"s3://{S3_BUCKET}/external-staging/"
    logger.info(f"Syncing {external_dir.name}/ → {s3_staging} (staging)")
    _s3_sync(str(external_dir), s3_staging)
    logger.info("External cache pushed to staging")


@task(name="promote-external-staging", log_prints=True)
def promote_external_staging() -> None:
    """Server-side copy ``external-staging/`` → ``external/`` in S3.

    Called after the fetch flow has validated its output.  Uses S3
    ``CopyObject`` so no data travels through the client and the promotion
    is as fast as possible.  After this call, ``external/`` contains the
    complete, validated snapshot and any subsequent ``pipeline.py`` run
    that syncs from ``external/`` will see consistent data.

    No-op when ``S3_BUCKET`` is empty (local-only mode).
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping staging promotion (local mode)")
        return
    logger.info(
        f"Promoting s3://{S3_BUCKET}/external-staging/ → s3://{S3_BUCKET}/external/"
    )
    count = _s3_copy_prefix(S3_BUCKET, "external-staging/", "external/")
    logger.info(f"Promoted {count} object(s) from staging to live external cache")
