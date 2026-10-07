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

import contextvars
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import structlog

import docker as docker_sdk
from prefect import get_run_logger, task
from prefect.runtime import flow_run

# ── Constants ──────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parents[3]

# Relative path to the compiled JAR (from REPO_ROOT).
# The JAR is downloaded from S3 by ``ensure_jar()`` in flows/pipeline.py, or
# built locally with ``mvn clean package -DskipTests`` for development.
CLASSPATH = "target/nlm-ckn-etl-1.0.jar"

# Default Java heap.  Raise with --java-opts if OOM-killed (exit 137).
DEFAULT_JAVA_OPTS = "-Xmx32g"

ARANGO_DB_HOST = os.getenv("ARANGO_DB_HOST", "localhost")
# Loopback is local: the start/dump/restore tasks manage a local Docker
# container and must run for 127.0.0.1 and ::1, not just the literal "localhost".
ARANGO_DB_IS_LOCAL = ARANGO_DB_HOST in ("localhost", "127.0.0.1", "::1", "")
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
# KMS key ARN/ID for server-side encryption of S3 uploads.  Required in
# deployed environments; empty string falls back to SSE-S3 (AES-256).
S3_KMS_KEY_ID = os.getenv("S3_KMS_KEY_ID", "")

# PYTHONPATH injected into every direct Python script invocation so that
# sibling imports (LoaderUtilities, ArangoDbUtilities, …) resolve correctly.
PYTHON_SRC = str(REPO_ROOT / "python" / "src")


_log = logging.getLogger(__name__)
_events = structlog.get_logger(__name__)

# ── Private helpers ────────────────────────────────────────────────────────


def _get_or_create_arango_password() -> str:
    """Return the ArangoDB root password.

    Priority:
    1. ``ARANGO_DB_PASSWORD`` env var — used on AWS where the password is
       injected via the task definition or Secrets Manager.
    2. AWS Secrets Manager — fetched when ``PROJECT_NAME`` and
       ``ENVIRONMENT`` env vars are set, using the secret ID
       ``/<PROJECT_NAME>/<ENVIRONMENT>/secrets/arangodb-password``.
       This ensures the ETL starts ArangoDB with the same password that
       the UI's deploy pipeline expects, so the ``_users`` system
       collection in the dump is consistent on restore.
    3. ``.arangodb-password`` file in the repo root — used locally.
    4. Generate a new random password and write it to the file.
    """
    env_password = os.getenv("ARANGO_DB_PASSWORD")
    if env_password:
        return env_password

    project_name = os.getenv("PROJECT_NAME")
    environment = os.getenv("ENVIRONMENT")
    if project_name and environment:
        secret_id = f"/{project_name}/{environment}/secrets/arangodb-password"
        try:
            client = boto3.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_id)
            return response["SecretString"]
        except Exception as e:
            _events.error(
                "arango_password_fetch_failed",
                secret_id=secret_id,
                error_type=type(e).__name__,
                error=str(e),
            )
            raise

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
    except docker_sdk.errors.DockerException as exc:
        _events.warning(
            "docker_unreachable", error_type=type(exc).__name__, error=str(exc)
        )
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


# Used when a launch happens outside a Prefect run with no CORRELATION_ID set,
# so every subprocess of one process still shares a single id.
_FALLBACK_CORRELATION_ID = str(uuid.uuid4())


def _log_env(phase: str | None = None) -> dict[str, str]:
    """Return the logging context to merge into a subprocess environment.

    The worker scripts and Java run as separate processes, so the run-level
    context reaches them through the environment (see
    ``logging_setup.configure_logging``).

    ``CORRELATION_ID`` is, in order: the value already in the environment
    (so a caller or the Batch job definition can fix one id for the whole
    run), the id this process is already logging under (bound by
    ``configure_logging``, so the parent and its children join), the current
    Prefect flow-run id, or a per-process UUID. ``GIT_SHA`` is forwarded only
    when set; the child reports ``unknown`` otherwise.

    Parameters
    ----------
    phase:
        The pipeline phase the subprocess belongs to (``fetch``, ``ontology``,
        ``results`` or ``archive``). Omitted when ``None``, which leaves any
        ``PHASE`` already in the environment untouched.
    """
    bound = structlog.contextvars.get_contextvars().get("correlation_id")
    env = {
        "CORRELATION_ID": os.environ.get("CORRELATION_ID")
        or bound
        or flow_run.id
        or _FALLBACK_CORRELATION_ID
    }
    git_sha = os.environ.get("GIT_SHA")
    if git_sha:
        env["GIT_SHA"] = git_sha
    if phase:
        env["PHASE"] = phase
    return env


# Longest child line forwarded as one event; CloudWatch rejects events over
# 256 KB, and a runaway line is not worth the whole batch.
_MAX_CHILD_LINE = 32_768

# How long to keep waiting for a child's pipes to close after the child itself
# has exited. A descendant it left running (a build daemon, say) can hold them
# open indefinitely; its output is still forwarded, but the flow moves on.
_READER_GRACE_S = 5.0


def _forward_child_line(line: str, stream: str, truncated: bool = False) -> None:
    """Emit one line of child output as a structured log line.

    A line that is already a structured record (a JSON object with ``message``
    and ``level``, as the Python workers and Java write) is written through
    unchanged, so its fields and level survive. Anything else (a JVM stack
    trace, Maven output, a third-party warning) becomes a ``subprocess_output``
    event: INFO for stdout, WARN for stderr.

    Parameters
    ----------
    line:
        One line of child output, without its newline.
    stream:
        ``stdout`` or ``stderr``.
    truncated:
        The line was longer than ``_MAX_CHILD_LINE`` and ``line`` is its
        prefix. A truncated line is never passed through, even when it looks
        structured, because the sink may reject an oversized event.
    """
    if not truncated and line.startswith("{"):
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if isinstance(record, dict) and "message" in record and "level" in record:
            # One write call, so lines from the two reader threads and the
            # parent's own handler cannot interleave mid-line.
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            return
    fields = {"stream": stream, "line": line}
    if truncated:
        fields["truncated"] = True
    if stream == "stderr":
        _events.warning("subprocess_output", **fields)
    else:
        _events.info("subprocess_output", **fields)


def _read_lines(pipe):
    """Yield ``(line, truncated)`` from ``pipe`` without buffering a whole line.

    At most ``_MAX_CHILD_LINE`` characters of a line are kept; the rest of an
    over-long line is read and discarded up to its newline, so a child that
    never writes one cannot grow this process's memory.
    """
    while True:
        chunk = pipe.readline(_MAX_CHILD_LINE + 1)
        if not chunk:
            return
        truncated = len(chunk) > _MAX_CHILD_LINE and not chunk.endswith("\n")
        if truncated:
            while True:
                rest = pipe.readline(_MAX_CHILD_LINE + 1)
                if not rest or rest.endswith("\n"):
                    break
        yield chunk.rstrip("\r\n")[:_MAX_CHILD_LINE], truncated


def _pump(pipe, stream: str) -> None:
    """Forward every line of ``pipe`` until it closes."""
    with pipe:
        for line, truncated in _read_lines(pipe):
            if line.strip():
                _forward_child_line(line, stream, truncated)


def _run_logged(
    cmd: list[str],
    phase: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> None:
    """Run a subprocess, logging its lifecycle and forwarding its output.

    Emits ``subprocess_started``, then ``subprocess_finished`` (``rc``,
    ``duration_ms``) or ``subprocess_failed`` (``reason=nonzero_exit``, ``rc``,
    ``duration_ms``), and raises :class:`subprocess.CalledProcessError` on a
    non-zero exit just as ``subprocess.run(check=True)`` does. The child is
    killed if this call is interrupted.

    The child's stdout and stderr are read line by line, never accumulated;
    see :func:`_forward_child_line` for how each line is forwarded. With
    ``LOG_FORMAT=console`` the child inherits this process's streams instead,
    since both sides are then writing human-readable text.

    Parameters
    ----------
    cmd:
        The command and arguments. Logged as ``argv``, so do not put secrets
        in it; pass them through ``env``.
    phase:
        The pipeline phase; merged into the child environment as ``PHASE``
        together with ``CORRELATION_ID`` and ``GIT_SHA`` (see :func:`_log_env`).
    env:
        The child environment. Defaults to this process's environment.
    cwd:
        Working directory for the child.
    """
    child_env = {**(os.environ if env is None else env), **_log_env(phase)}
    capture = os.environ.get("LOG_FORMAT", "").lower() != "console"
    pipe = subprocess.PIPE if capture else None
    fields = {"phase": phase, "argv": cmd}
    if cwd is not None:
        fields["cwd"] = str(cwd)
    _events.info("subprocess_started", **fields)
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        env=child_env,
        cwd=cwd,
        stdout=pipe,
        stderr=pipe,
        text=True,
        errors="replace",
        bufsize=1,
    )
    readers = []
    if capture:
        # A new thread starts with an empty context, so give each reader a copy
        # of this one; otherwise its events lose the correlation id.
        readers = [
            threading.Thread(
                target=contextvars.copy_context().run,
                args=(_pump, pipe_, name),
                daemon=True,
            )
            for pipe_, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))
        ]
    try:
        for reader in readers:
            reader.start()
        rc = proc.wait()
        deadline = time.monotonic() + _READER_GRACE_S
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()))
    except BaseException:
        proc.kill()
        proc.wait()
        # A grandchild holding the pipe open must not hang the cleanup.
        for reader in readers:
            reader.join(timeout=5)
        raise
    if any(reader.is_alive() for reader in readers):
        # The daemon readers keep forwarding whatever the descendant writes.
        _events.warning("subprocess_pipes_held", phase=phase, grace_s=_READER_GRACE_S)
    duration_ms = int((time.monotonic() - started) * 1000)
    if rc != 0:
        failure = {"reason": "nonzero_exit", "rc": rc, "duration_ms": duration_ms}
        if rc in (137, -9):
            failure["oom_suspected"] = True
        _events.error("subprocess_failed", phase=phase, **failure)
        raise subprocess.CalledProcessError(rc, cmd)
    _events.info("subprocess_finished", phase=phase, rc=rc, duration_ms=duration_ms)


def _run_python_script(
    script: str,
    arango_db_password: str,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
    phase: str | None = None,
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
    phase:
        The pipeline phase, forwarded as ``PHASE`` (see :func:`_log_env`).
    """
    env = {
        **os.environ,
        "PYTHONPATH": PYTHON_SRC,
        "PYTHONUNBUFFERED": "1",
        **_arango_env(arango_db_password),
        **(extra_env or {}),
    }
    _run_logged(
        [
            sys.executable,
            str(REPO_ROOT / "python" / "src" / script),
            *(extra_args or []),
        ],
        phase=phase,
        env=env,
    )


def _parse_s3_url(s3_url: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``."""
    without_scheme = s3_url[len("s3://") :]
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


def _fields(**fields) -> dict:
    """Return ``fields`` without the ``None`` values, so absent ones are not logged."""
    return {k: v for k, v in fields.items() if v is not None}


def _s3_upload_tar(
    local_dir: Path, s3_path: str, artifact: str | None = None
) -> dict[str, int]:
    """Compress ``local_dir`` to a .tar.gz and upload to ``s3_path``.

    Produces a single object with a stable hash, reducing per-file S3 API
    overhead and enabling integrity checking.  No-op when ``S3_BUCKET`` is empty.

    Security note: uploads always use server-side encryption (SSE-KMS when
    S3_KMS_KEY_ID is set, otherwise SSE-S3/AES-256).  Bucket-level public-access
    blocks and S3/CloudTrail logging must also be enforced via infrastructure
    policy — this call alone is not sufficient.

    Logs ``s3_upload_finished`` (the URI, whether KMS was used, never the key
    id).  Members under ``.archive/`` are left out of the archive and counted.

    Parameters
    ----------
    local_dir:
        Directory to archive.
    s3_path:
        Destination ``s3://bucket/key``.
    artifact:
        Optional artifact name for the log line (``tuples``, ``baseline_dump``, ...).

    Returns
    -------
    dict
        ``files`` archived, ``members_skipped`` and ``bytes`` uploaded; all zero
        when ``S3_BUCKET`` is empty.
    """
    if not S3_BUCKET:
        return {"files": 0, "members_skipped": 0, "bytes": 0}
    started = time.monotonic()
    local_dir = Path(local_dir)
    bucket, key = _parse_s3_url(s3_path)
    sse_args: dict = (
        {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": S3_KMS_KEY_ID}
        if S3_KMS_KEY_ID
        else {"ServerSideEncryption": "AES256"}
    )
    counts = {"files": 0, "members_skipped": 0}

    def _keep(member):
        if "/.archive/" in member.name:
            counts["members_skipped"] += 1
            return None
        if member.isfile():
            counts["files"] += 1
        return member

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(local_dir, arcname=local_dir.name, filter=_keep)
        size = tmp_path.stat().st_size
        boto3.client("s3").upload_file(
            str(tmp_path), bucket, key, ExtraArgs={"ACL": "private", **sse_args}
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    stats = {**counts, "bytes": size}
    _events.info(
        "s3_upload_finished",
        **_fields(artifact=artifact),
        s3_uri=s3_path,
        kms_encrypted=bool(S3_KMS_KEY_ID),
        duration_ms=int((time.monotonic() - started) * 1000),
        **stats,
    )
    return stats


def _s3_download_tar(
    s3_path: str, local_dir: Path, artifact: str | None = None
) -> dict[str, int]:
    """Download a .tar.gz from ``s3_path`` and extract its contents into ``local_dir``.

    The top-level directory inside the archive is stripped so that files land
    directly in ``local_dir`` (mirrors the extraction pattern used in
    ``dump_arangodb``).  No-op when ``S3_BUCKET`` is empty.

    Logs ``s3_download_finished``.  Symlinks, hard links and members that would
    resolve outside ``local_dir`` are not extracted; when any are skipped a
    ``tar_members_skipped`` WARN reports how many of each.

    Parameters
    ----------
    s3_path:
        Source ``s3://bucket/key``.
    local_dir:
        Directory to extract into.
    artifact:
        Optional artifact name for the log line.

    Returns
    -------
    dict
        ``files`` extracted, ``links_skipped``, ``path_traversal_skipped`` and
        ``bytes`` downloaded; all zero when ``S3_BUCKET`` is empty.
    """
    if not S3_BUCKET:
        return {"files": 0, "links_skipped": 0, "path_traversal_skipped": 0, "bytes": 0}
    started = time.monotonic()
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    bucket, key = _parse_s3_url(s3_path)
    counts = {"files": 0, "links_skipped": 0, "path_traversal_skipped": 0}
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        boto3.client("s3").download_file(bucket, key, str(tmp_path))
        size = tmp_path.stat().st_size
        base_dir = local_dir.resolve()
        with tarfile.open(tmp_path, "r:gz") as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts
                if len(parts) <= 1:
                    continue
                if member.issym() or member.islnk():
                    counts["links_skipped"] += 1
                    continue
                member.name = str(Path(*parts[1:]))
                resolved = (local_dir / member.name).resolve()
                if not str(resolved).startswith(str(base_dir) + os.sep):
                    counts["path_traversal_skipped"] += 1
                    continue
                tar.extract(member, local_dir)
                if member.isfile():
                    counts["files"] += 1
    finally:
        tmp_path.unlink(missing_ok=True)
    stats = {**counts, "bytes": size}
    if counts["links_skipped"] or counts["path_traversal_skipped"]:
        _events.warning(
            "tar_members_skipped",
            **_fields(artifact=artifact),
            s3_uri=s3_path,
            links=counts["links_skipped"],
            path_traversal=counts["path_traversal_skipped"],
        )
    _events.info(
        "s3_download_finished",
        **_fields(artifact=artifact),
        s3_uri=s3_path,
        duration_ms=int((time.monotonic() - started) * 1000),
        **stats,
    )
    return stats


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


def _s3_sync(src: str, dst: str, artifact: str | None = None) -> dict[str, int]:
    """Sync a directory between local filesystem and S3, skipping unchanged files.

    Detects direction from whether ``src`` or ``dst`` starts with ``s3://``.
    Unchanged files are identified by size, matching ``aws s3 sync`` behaviour.
    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Logs ``s3_sync_finished`` with the direction (``down`` or ``up``), the S3
    URI and the counts.

    Parameters
    ----------
    src, dst:
        One is an ``s3://bucket/prefix``, the other a local directory.
    artifact:
        Optional artifact name for the log line.

    Returns
    -------
    dict
        ``objects_transferred``, ``objects_skipped`` (unchanged) and ``bytes``
        transferred; all zero when ``S3_BUCKET`` is empty.
    """
    stats = {"objects_transferred": 0, "objects_skipped": 0, "bytes": 0}
    if not S3_BUCKET:
        return stats
    started = time.monotonic()
    s3 = boto3.client("s3")
    if src.startswith("s3://"):
        # Download: S3 → local
        direction, s3_uri = "down", src
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
                    stats["objects_transferred"] += 1
                    stats["bytes"] += obj["Size"]
                else:
                    stats["objects_skipped"] += 1
    else:
        # Upload: local → S3
        direction, s3_uri = "up", dst
        local_dir = Path(src)
        bucket, prefix = _parse_s3_url(dst)
        sse_args = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": S3_KMS_KEY_ID}
            if S3_KMS_KEY_ID
            else {"ServerSideEncryption": "AES256"}
        )
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
            size = path.stat().st_size
            if relative not in s3_index or s3_index[relative] != size:
                s3.upload_file(
                    str(path),
                    bucket,
                    prefix + relative,
                    ExtraArgs={"ACL": "private", **sse_args},
                )
                stats["objects_transferred"] += 1
                stats["bytes"] += size
            else:
                stats["objects_skipped"] += 1
    _events.info(
        "s3_sync_finished",
        **_fields(artifact=artifact),
        direction=direction,
        s3_uri=s3_uri,
        duration_ms=int((time.monotonic() - started) * 1000),
        **stats,
    )
    return stats


# ── GitHub deployment status ───────────────────────────────────────────────


def _cloudwatch_log_url() -> str:
    """Return a CloudWatch console URL for the current Batch job, or empty string."""
    job_id = os.getenv("AWS_BATCH_JOB_ID", "")
    if not job_id:
        return ""
    region    = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    log_group = "/batch/nlm-ckn-release"
    encoded   = log_group.replace("/", "$252F")
    return (
        f"https://console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{encoded}"
    )


def post_github_deployment_status(*, state: str, description: str) -> None:
    """POST a deployment status to the GitHub Deployments API.

    Reads ``GITHUB_TOKEN``, ``GITHUB_REPOSITORY``, and ``GITHUB_DEPLOYMENT_ID``
    from the environment.  Silently no-ops if any are absent or the request
    fails, so a notification error never masks the real pipeline outcome.

    Parameters
    ----------
    state:
        One of ``"in_progress"``, ``"success"``, ``"failure"``, ``"error"``.
    description:
        Short human-readable summary shown on the deployments page (≤ 140 chars).
    """
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    deployment_id = os.getenv("GITHUB_DEPLOYMENT_ID", "")
    missing = [
        name
        for name, value in (
            ("GITHUB_TOKEN", token),
            ("GITHUB_REPOSITORY", repo),
            ("GITHUB_DEPLOYMENT_ID", deployment_id),
        )
        if not value
    ]
    if missing:
        _events.warning("github_status_skipped", state=state, missing_vars=missing)
        return

    payload: dict = {
        "state": state,
        "description": description[:140],
        "environment": "production",
    }
    log_url = _cloudwatch_log_url()
    if log_url:
        payload["log_url"] = log_url

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/deployments/{deployment_id}/statuses",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        _events.info(
            "github_status_posted",
            state=state,
            status_code=resp.status,
            log_url_attached=bool(log_url),
        )
    except urllib.error.HTTPError as exc:
        _events.warning(
            "github_status_failed",
            state=state,
            reason="http_error",
            status_code=exc.code,
            error=exc.read().decode(errors="replace")[:500],
        )
    except Exception as exc:
        _events.warning(
            "github_status_failed",
            state=state,
            error_type=type(exc).__name__,
            error=str(exc),
        )


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


# Files whose content determines what or how external data is fetched.  A change
# to any of them invalidates a cached external fetch (see ``should_force_fetch``).
_FETCH_CODE_FILES = (
    "python/src/DataFetcher.py",
    "python/src/DataTransformer.py",
    "python/src/E_Utilities.py",
    "python/src/flows/fetch.py",
)


def _fetch_code_hash() -> str:
    """Return a 16-char SHA-256 prefix over the fetch code files.

    Content-addressed so the key changes whenever the fetcher/transformer logic
    changes.  Written into ``fetch-info.json`` and compared on later runs to
    decide whether a cached external fetch is still valid.
    """
    h = hashlib.sha256()
    for rel in _FETCH_CODE_FILES:
        p = REPO_ROOT / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:16]


def should_force_fetch(
    run_name: str = "", max_fetch_age_hours: float = 672.0, log=None
) -> bool:
    """Decide whether the external fetch should force a full re-fetch.

    Returns ``True`` only when the cached fetch is genuinely untrustworthy: it
    was produced by different fetch code (``fetch_code_hash`` no longer matches)
    or is older than ``max_fetch_age_hours``.  A missing or corrupt marker
    returns ``False`` (resume) — the per-source caches on disk are reused and
    only missing/failed entries are re-fetched — so a run that fetched data but
    failed validation (or was interrupted before recording a marker) is not
    discarded and re-fetched from scratch on the next attempt.

    Reads ``fetch-info.json`` from S3 (when ``S3_BUCKET`` is set) or from the
    local ``data/external-<name>/`` directory.

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/`` in local mode).
    max_fetch_age_hours:
        Maximum acceptable cache age in hours before forcing a re-fetch.
        Defaults to 672 (four weeks).
    log:
        Optional ``callable(str)`` for progress messages (defaults to the
        module logger so this works outside a Prefect run context).
    """
    log = log or _log.info
    fetch_info = None

    if S3_BUCKET:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            boto3.client("s3").download_file(
                S3_BUCKET, "external/fetch-info.json", str(tmp_path)
            )
            fetch_info = json.loads(tmp_path.read_text())
        except Exception as exc:
            log(f"Could not read fetch-info.json from S3: {exc}")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
    else:
        info_path = _external_dir(run_name) / "fetch-info.json"
        if info_path.exists():
            try:
                fetch_info = json.loads(info_path.read_text())
            except Exception as exc:
                log(f"Could not parse fetch-info.json: {exc}")

    if fetch_info is None:
        # No marker (first-ever fetch, or a prior run failed / was interrupted
        # before recording one).  Resume rather than wipe: the per-source caches
        # on disk are reused and only missing/failed entries are re-fetched (an
        # empty cache simply fetches everything).
        log("No fetch-info.json found — resuming from on-disk cache")
        return False

    # Force if the fetch code changed since this cache was produced — even a
    # fresh cache is invalid if the fetcher/transformer logic has changed.
    cached_hash = fetch_info.get("fetch_code_hash")
    current_hash = _fetch_code_hash()
    if cached_hash != current_hash:
        log(
            f"Fetch code changed since cache was written "
            f"(cached={cached_hash}, current={current_hash}) — forcing full re-fetch"
        )
        return True

    try:
        fetched_at = datetime.fromisoformat(fetch_info["fetched_at"])
    except (KeyError, TypeError, ValueError) as exc:
        log(
            f"Missing/invalid fetched_at in fetch-info.json ({exc!r}) — "
            "resuming from on-disk cache"
        )
        return False
    if fetched_at.tzinfo is None:  # tolerate naive timestamps
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600

    if age_hours > max_fetch_age_hours:
        log(
            f"External cache is {age_hours:.1f}h old "
            f"(threshold: {max_fetch_age_hours}h) — forcing full re-fetch"
        )
        return True

    log(
        f"External cache is {age_hours:.1f}h old (threshold: {max_fetch_age_hours}h)"
        " — reusing cache, retrying any previous failures"
    )
    return False


def _external_dir(run_name: str = "") -> Path:
    """Return the run-specific external cache directory.

    Mirrors ``RunConfig.external_dir`` (``data/external-{run_name}``) without
    importing ``LoaderUtilities`` (which pulls in heavy scientific packages).
    """
    import os as _os

    run_name = run_name or _os.getenv("CKN_RUN", "full")
    return REPO_ROOT / "data" / f"external-{run_name}"


@task(name="clean-empty-external-files", log_prints=True)
def clean_empty_external_files(run_name: str = "") -> None:
    """Remove corrupt or structurally invalid files from ``data/external-<name>/``.

    ``DataFetcher.py`` uses cache files in ``data/external-<name>/``
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
    run_name:
        Run name (selects ``data/external-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    external_dir = _external_dir(run_name)
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
def validate_external_files(run_name: str = "") -> None:
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
    run_name:
        Run name (selects ``data/external-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    external_dir = _external_dir(run_name)
    raw_required = [
        "cellxgene.json",
        "opentargets.json",
        "gene.json",
        "uniprot.json",
    ]
    transformed_required = [
        "cellxgene_transformed.json",
        "opentargets_transformed.json",
        "gene_transformed.json",
        "uniprot_transformed.json",
    ]

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
                if not data:
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
def sync_external_from_s3(run_name: str = "") -> None:
    """Restore the external API cache from S3 to ``data/external-<name>/``.

    No-op when ``S3_BUCKET`` is empty (local-only mode).  Used by both the
    fetch flow (to resume an interrupted run) and the pipeline flow (to pull
    the cache that the fetch flow produced).

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/`` locally; the S3 cache
        prefix is the live, shared ``external/``).  Defaults to ``$CKN_RUN``
        or ``'full'``.
    """
    if not S3_BUCKET:
        return
    external_dir = _external_dir(run_name)
    external_dir.mkdir(parents=True, exist_ok=True)
    _s3_sync(f"s3://{S3_BUCKET}/external/", str(external_dir), artifact="external")


@task(name="sync-external-to-s3", log_prints=True)
def sync_external_to_s3(run_name: str = "") -> None:
    """Push the external API cache from ``data/external-<name>/`` to S3.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/`` locally; the S3 cache
        prefix is the live, shared ``external/``).  Defaults to ``$CKN_RUN``
        or ``'full'``.
    """
    if not S3_BUCKET:
        return
    external_dir = _external_dir(run_name)
    _s3_sync(str(external_dir), f"s3://{S3_BUCKET}/external/", artifact="external")


@task(name="sync-external-to-s3-staging", log_prints=True)
def sync_external_to_s3_staging(run_name: str = "") -> None:
    """Push the external API cache to the run-scoped staging prefix.

    Writes to ``s3://{S3_BUCKET}/runs/<name>/external-staging/`` rather than
    the live ``external/`` prefix so that a concurrent ``pipeline.py`` reading
    from ``external/`` sees only complete, validated snapshots.  Call
    ``promote_external_staging`` after validation to atomically swap the
    staging data into the live prefix.

    Scoping staging to the run directory means two concurrent releases never
    overwrite each other's in-flight data.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/`` locally and
        ``runs/<name>/external-staging/`` in S3).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    if not S3_BUCKET:
        return
    run_name = run_name or os.getenv("CKN_RUN", "full")
    external_dir = _external_dir(run_name)
    s3_staging = f"s3://{S3_BUCKET}/runs/{run_name}/external-staging/"
    _s3_sync(str(external_dir), s3_staging, artifact="external_staging")


@task(name="promote-external-staging", log_prints=True)
def promote_external_staging(run_name: str = "") -> None:
    """Server-side copy ``runs/<name>/external-staging/`` → ``external/`` in S3.

    Called after the fetch flow has validated its output.  Uses S3
    ``CopyObject`` so no data travels through the client and the promotion
    is as fast as possible.  After this call, ``external/`` contains the
    complete, validated snapshot and any subsequent ``pipeline.py`` run
    that syncs from ``external/`` will see consistent data.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name (must match the value passed to ``sync_external_to_s3_staging``).
        Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    if not S3_BUCKET:
        return
    run_name = run_name or os.getenv("CKN_RUN", "full")
    src_prefix = f"runs/{run_name}/external-staging/"
    started = time.monotonic()
    count = _s3_copy_prefix(S3_BUCKET, src_prefix, "external/")
    # Nothing to promote means the live cache was not refreshed: surface it.
    log = _events.info if count else _events.warning
    log(
        "external_cache_promoted",
        objects_copied=count,
        src_prefix=src_prefix,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
