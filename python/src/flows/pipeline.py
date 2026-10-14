#!/usr/bin/env python3
"""Prefect ETL pipeline for the NLM-CKN project.

Reads external API data (produced by ``fetch.py``) from S3 or the local
filesystem, processes it through tuple writers, loads the results into
ArangoDB, and promotes a golden database dump to production S3.

Three-phase architecture
------------------------
The pipeline is split into three phases, each of which restores its
predecessor's ``arangodump`` save point and writes its own, so any single
phase can be re-run on its own: Phase 1 writes the baseline dump, Phase 2
the results dump, and Phase 3 the golden dump:

**Phase 1 — Upstream Build** (``--run-ontology``):
  Download OWL ontologies → slim → load into ArangoDB → ``arangodump``
  the resulting database to ``data/arangodump-baseline-<jar>/``.  This phase
  is expensive but only needs to rerun when ontologies change.  Once
  the baseline dump exists, Phase 1 is skipped automatically on subsequent
  invocations unless ``--force-ontology`` is passed.

**Phase 2 — Iterative Refinement** (``--run-results``):
  ``arangorestore`` the baseline dump → write all tuples → build the results
  graph → create ontology analyzers and views → ``arangodump`` the results
  dump to ``data/arangodump-results-<jar>-<name>/``.  Because it restores from
  the baseline each time, Phase 2 is fully repeatable without re-running the
  expensive ontology build.  Skipped when the results dump already exists
  unless ``--force-results`` is passed.

**Phase 3 — Production Handoff** (``--run-archive``):
  Restore the results dump (when Phase 2 did not just run) → build the induced
  phenotype subgraph → create phenotype analyzers and views → ``arangodump``
  the golden state to ``data/arangodump-golden-<name>/``, then sync all
  production artifacts (dump, OBO files, external cache snapshot, build-info)
  to ``s3://${S3_BUCKET}/runs/<name>/`` (stages 02–06 + build-info.txt).
  Skipped when the golden dump already exists unless ``--force-archive`` is
  passed.

Prerequisites
-------------
Run ``flows/fetch.py`` first (or ensure ``data/external-<name>/`` contains fresh
cache files), then::

    cd python
    python src/flows/pipeline.py --help
    python src/flows/pipeline.py --run-ontology
    python src/flows/pipeline.py --run-results
    python src/flows/pipeline.py --run-archive
    python src/flows/pipeline.py --run-ontology --run-results --run-archive

Or with the Prefect CLI after ``prefect server start``::

    prefect deployment run 'nlm-ckn-etl/local'

S3 mode
-------
Set ``S3_BUCKET`` to pull inputs from S3 before processing and push
production artifacts to S3 after the archive phase::

    S3_BUCKET=cell-kn-arangodb-data-952291113202 python src/flows/pipeline.py --run-results

JAR
---
The Java programs (OntologyDownloader, OntologyGraphBuilder, etc.) require a
pre-built JAR at ``target/nlm-ckn-etl-1.0.jar``.  The JAR is produced once
by CI/CD (see ``.github/workflows/build-jar.yml``) and stored in S3.  The
``ensure_jar`` task downloads it automatically when ``S3_BUCKET`` is set, or
you can build it locally with::

    mvn clean package -DskipTests

See the README for full instructions.
"""

import argparse
import io
import json
import tempfile
import os
import re
import shutil
import sys
import tarfile
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import boto3

import docker as docker_sdk
import structlog
from prefect import flow, task

import _common as _common_mod
from _common import (
    ARANGO_DB_HOME,
    ARANGO_DB_HOST,
    ARANGO_DB_HOST_HOME,
    ARANGO_DB_IS_LOCAL,
    ARANGO_DB_PORT,
    ARANGO_DB_VOLUME_NAME,
    CLASSPATH,
    DEFAULT_JAVA_OPTS,
    PYTHON_SRC,
    REPO_ROOT,
    S3_BUCKET,
    S3_KMS_KEY_ID,
    _arango_env,
    _external_dir,
    _find_free_port,
    _fetch_code_hash,
    _fields,
    _git_commit,
    _get_arangodb_id,
    _get_or_create_arango_password,
    _jar_key,
    _log_env,
    _parse_s3_url,
    _run_logged,
    _run_python_script,
    _s3_download_tar,
    _s3_sync,
    _s3_upload_tar,
    post_github_deployment_status,
    sync_external_from_s3,
    validate_external_files,
)

# Put python/src on the path so in-process tasks can import sibling modules
# (e.g. ArangoDbUtilities) the same way _run_python_script subprocesses do.
sys.path.insert(0, PYTHON_SRC)

_events = structlog.get_logger(__name__)

# arangodump/arangorestore output kept at DEBUG is cut to this many characters.
_MAX_TOOL_OUTPUT = 32_768
_ERROR_LINE = re.compile(r"\bERROR\b")


def _ms(started: float) -> int:
    """Return the whole milliseconds elapsed since ``started`` (monotonic)."""
    return int((time.monotonic() - started) * 1000)


def _dir_bytes(path: Path) -> int:
    """Return the total size in bytes of the files under ``path`` (0 if absent)."""
    path = Path(path)
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@contextmanager
def _phase(name: str, summary: dict, **fields):
    """Log ``phase_started`` and ``phase_finished`` around a pipeline phase.

    Binds ``phase`` for every line logged inside and records the phase in
    ``summary`` (``phase`` is the one in progress, ``durations`` the finished
    ones).  Yields a dict the caller fills with the counts to add to
    ``phase_finished``.  No ``phase_finished`` is logged if the body raises; the
    flow logs ``run_failed`` instead.
    """
    started = time.monotonic()
    summary["phase"] = name
    _events.info("phase_started", phase=name, **fields)
    finish: dict = {}
    with structlog.contextvars.bound_contextvars(phase=name):
        yield finish
    duration_ms = _ms(started)
    summary["durations"][name] = duration_ms
    summary["phase"] = None
    _events.info("phase_finished", phase=name, duration_ms=duration_ms, **fields, **finish)


def _failing_task(exc: BaseException) -> str | None:
    """Return the name of the innermost function in this module on ``exc``'s stack.

    For an error raised inside a task this is the task's function, so
    ``run_failed`` can say which task failed.
    """
    name = None
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        if frame.f_code.co_filename == __file__:
            name = frame.f_code.co_name
    return name


def _log_source_versions(run_name: str) -> None:
    """Log ``source_versions_resolved`` for each source that recorded a version.

    Reads ``fetch-status.json``, which ``DataFetcher.py`` writes.  Logs nothing
    for a source without a ``source_version``.
    """
    status_path = _external_dir(run_name) / "fetch-status.json"
    if not status_path.exists():
        return
    try:
        status = json.loads(status_path.read_text())
    except Exception as exc:
        _events.warning(
            "fetch_status_unreadable",
            path=status_path.name,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return
    for source, entry in status.items():
        if isinstance(entry, dict) and entry.get("source_version"):
            _events.info(
                "source_versions_resolved",
                source=source,
                source_version=entry["source_version"],
            )

# ── Tasks ──────────────────────────────────────────────────────────────────


@task(name="stop-arangodb", log_prints=True)
def stop_arangodb() -> None:
    """Stop and remove the running ArangoDB container, if any."""
    try:
        client = docker_sdk.from_env()
    except docker_sdk.errors.DockerException as exc:
        _events.warning(
            "docker_unreachable", error_type=type(exc).__name__, error=str(exc)
        )
        return
    containers = client.containers.list(filters={"name": "arangodb"}, all=True)
    for container in containers:
        status = container.status
        if status == "running":
            container.stop()
        container.remove()
        _events.info(
            "arangodb_container_removed",
            container_id=container.short_id,
            status=status,
        )


def _wipe_arangodb_data(arango_db_home: str) -> None:
    """Remove all persisted ArangoDB data so the next start initialises fresh.

    For bind-mount volumes, removes the local directory.
    For named Docker volumes (used when running inside a container without
    ARANGO_DB_HOST_HOME set), removes the volume so ArangoDB re-initialises
    with the current ARANGO_ROOT_PASSWORD instead of the one baked into
    the old volume.
    """
    volume_source, is_named_volume = _arangodb_volume_source(arango_db_home)
    if is_named_volume:
        try:
            client = docker_sdk.from_env()
            try:
                vol = client.volumes.get(volume_source)
                vol.remove(force=True)
                _events.info(
                    "arangodb_data_wiped", kind="named_volume", volume=volume_source
                )
            except docker_sdk.errors.NotFound:
                pass  # nothing to remove
        except docker_sdk.errors.DockerException as exc:
            # Log and continue: a stale volume keeps the old root password and
            # causes a 401 when the next container starts.
            _events.warning(
                "arangodb_volume_removal_failed",
                volume=volume_source,
                error_type=type(exc).__name__,
                error=str(exc),
            )
    else:
        arango_home = Path(volume_source)
        if arango_home.exists():
            shutil.rmtree(arango_home)
            _events.info(
                "arangodb_data_wiped", kind="bind_mount", path=str(arango_home)
            )


def _arangodb_volume_source(arango_db_home: str) -> tuple[str, bool]:
    """Return the Docker volume source for ArangoDB data and whether it is a named volume.

    When the pipeline runs inside a Docker container (detected by
    ``/.dockerenv``) and ``ARANGO_DB_HOST_HOME`` is not set, the Docker SDK
    sends volume mounts to the *host* daemon.  Container-internal paths like
    ``/app/data/arangodb`` are unknown to the host, causing a
    "path not shared from host" error.  In that case a named Docker volume is
    used instead — the host daemon manages it without needing a host path.

    Priority order:
    1. ``ARANGO_DB_HOST_HOME`` env var — explicit host-side bind-mount path.
    2. Running inside a container (``/.dockerenv`` present) → named volume.
    3. Otherwise → ``arango_db_home`` directly (direct host execution).

    Returns a ``(source, is_named_volume)`` tuple.
    """
    if ARANGO_DB_HOST_HOME:
        return ARANGO_DB_HOST_HOME, False
    if Path("/.dockerenv").exists():
        return ARANGO_DB_VOLUME_NAME, True
    return arango_db_home, False


def _wait_for_arangodb_ready(
    port: int, arango_db_password: str, timeout: float = 120.0
) -> None:
    """Block until ArangoDB accepts authenticated connections on the host port.

    ``start_arangodb`` launches the container detached; ArangoDB then needs
    several seconds to initialise a freshly-wiped data dir (create ``_system``,
    apply the root password) before it accepts connections.  ``arangorestore``/
    ``arangodump`` connect immediately afterwards, so without this wait they hit
    "cannot create server connection" against a still-initialising server (its
    own ~3s retry is not enough for a cold start on a fresh data dir).

    Polls ``GET /_api/version`` with root credentials until it returns 200 or
    ``timeout`` seconds elapse (then raises).
    """
    import base64
    import time
    import urllib.error
    import urllib.request

    auth = base64.b64encode(f"root:{arango_db_password}".encode()).decode()
    req = urllib.request.Request(
        f"http://{ARANGO_DB_HOST}:{port}/_api/version",
        headers={"Authorization": f"Basic {auth}"},
    )
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return
                last_err = RuntimeError(f"HTTP {resp.status}")
        except (urllib.error.URLError, OSError) as exc:
            # Connection refused while the server is still starting, or a
            # transient 401 before the root password is applied — keep polling.
            last_err = exc
        time.sleep(1.0)
    raise RuntimeError(
        f"ArangoDB did not become ready on {ARANGO_DB_HOST}:{port} "
        f"within {timeout:.0f}s (last error: {last_err})"
    )


@task(name="start-arangodb", log_prints=True)
def start_arangodb(arango_db_home: str, arango_db_password: str) -> int:
    """Start the ArangoDB container with the data directory mounted.

    Uses the Docker SDK (no ``docker`` CLI binary required).  The container
    runs detached; the caller is responsible for waiting until ArangoDB
    accepts connections before using it.  An OS-assigned free port is used
    to avoid conflicts with any process already bound to the default 8529.

    Returns the host port the container was bound to.

    Volume source selection (see ``_arangodb_volume_source``):
    - ``ARANGO_DB_HOST_HOME`` set → bind-mount that host path.
    - Running inside a container → named volume ``nlm-ckn-arangodb-data``.
    - Direct host execution → bind-mount ``arango_db_home``.
    """
    existing_id = _get_arangodb_id()
    if existing_id:
        client = docker_sdk.from_env()
        port = ARANGO_DB_PORT
        try:
            container = client.containers.get(existing_id)
            ports = container.ports.get("8529/tcp")
            if ports:
                port = int(ports[0]["HostPort"])
        except Exception as exc:
            # Fall back to the default port, but say so: a wrong port shows up
            # later as an unexplained connection failure.
            _events.warning(
                "arangodb_port_lookup_failed",
                container_id=existing_id,
                port=port,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        _events.info("arangodb_container_reused", container_id=existing_id, port=port)
        return port

    arango_db_port = _find_free_port()
    volume_source, is_named_volume = _arangodb_volume_source(arango_db_home)
    if not is_named_volume:
        Path(volume_source).mkdir(parents=True, exist_ok=True)
    volumes = {volume_source: {"bind": "/var/lib/arangodb3", "mode": "rw"}}

    started = time.monotonic()
    client = docker_sdk.from_env()
    client.containers.run(
        "arangodb",
        name="arangodb",
        detach=True,
        environment={"ARANGO_ROOT_PASSWORD": arango_db_password},
        ports={"8529/tcp": arango_db_port},
        volumes=volumes,
    )
    _events.info(
        "arangodb_container_started",
        port=arango_db_port,
        volume_kind="named_volume" if is_named_volume else "bind_mount",
        duration_ms=_ms(started),
    )
    # Wait until the server accepts authenticated connections before returning;
    # callers (dump/restore) connect immediately and a fresh data dir takes
    # several seconds to initialise.
    started = time.monotonic()
    _wait_for_arangodb_ready(arango_db_port, arango_db_password)
    _events.info("arangodb_ready", port=arango_db_port, duration_ms=_ms(started))
    return arango_db_port


@task(name="require-arangodb", log_prints=True)
def require_arangodb() -> None:
    """Verify ArangoDB is reachable before starting expensive tasks.

    Remote mode (``ARANGO_DB_HOST`` is not a loopback address): ArangoDB is
    managed externally (e.g. a dedicated EC2 instance).  Logs the endpoint
    and returns.

    Local mode: raises ``RuntimeError`` if no ArangoDB container is running.
    """
    if not ARANGO_DB_IS_LOCAL:
        _events.info(
            "arangodb_endpoint_resolved",
            mode="remote",
            host=ARANGO_DB_HOST,
            port=ARANGO_DB_PORT,
        )
        return
    cid = _get_arangodb_id()
    if not cid:
        raise RuntimeError(
            "ArangoDB container is not running. "
            "Start it first or run the ontology stage."
        )
    _events.info(
        "arangodb_endpoint_resolved",
        mode="local",
        host=ARANGO_DB_HOST,
        port=ARANGO_DB_PORT,
        container_id=cid,
    )


@task(name="ensure-jar", log_prints=True)
def ensure_jar() -> str:
    """Ensure the compiled JAR is present, downloading from S3 if necessary.

    The JAR is built once by CI/CD (``mvn clean package -DskipTests``) and
    stored at ``s3://${S3_BUCKET}/artifacts/nlm-ckn-etl-1.0.jar``.  This
    task downloads it on first run and reuses it on subsequent runs.

    When ``S3_BUCKET`` is unset (local-only mode), the JAR must already exist
    at ``target/nlm-ckn-etl-1.0.jar`` — build it with::

        mvn clean package -DskipTests

    Returns
    -------
    str
        A 16-character content hash of the JAR (see ``_jar_key``), used to
        key the baseline arangodump in S3 so the dump and the JAR that
        produced it are always stored together.
    """
    started = time.monotonic()
    jar = REPO_ROOT / CLASSPATH

    def _resolved(source: str) -> str:
        jar_key = _jar_key()
        _events.info(
            "jar_resolved",
            source=source,
            bytes=jar.stat().st_size,
            jar_key=jar_key,
            duration_ms=_ms(started),
        )
        return jar_key

    if jar.exists():
        return _resolved("cache")

    if not S3_BUCKET:
        _run_logged(["mvn", "clean", "package", "-DskipTests"], cwd=REPO_ROOT)
        if not jar.exists():
            raise FileNotFoundError(f"Maven build succeeded but JAR not found at {jar}")
        return _resolved("maven")

    jar.parent.mkdir(parents=True, exist_ok=True)
    s3_path = f"s3://{S3_BUCKET}/artifacts/{jar.name}"
    boto3.client("s3").download_file(S3_BUCKET, f"artifacts/{jar.name}", str(jar))
    if not jar.exists():
        raise FileNotFoundError(f"JAR download from {s3_path} failed")
    return _resolved("s3")


def _java_cmd(
    main_class: str,
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> list[str]:
    """Return the ``java`` command list for a given main class.

    Parameters
    ----------
    main_class:
        Fully-qualified Java class name (e.g. ``"gov.nih.nlm.OntologyDownloader"``).
    arango_db_password:
        ArangoDB root password.  Injected via env (not as a flag).
    java_opts:
        Space-separated JVM flags (e.g. ``"-Xmx32g"``).
    """
    return ["java"] + java_opts.split() + ["-cp", CLASSPATH, main_class]


@task(name="download-ontologies", log_prints=True)
def download_ontologies(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run OntologyDownloader to fetch OWL files into data/obo/."""
    started = time.monotonic()
    _run_logged(
        _java_cmd("gov.nih.nlm.OntologyDownloader", arango_db_password, java_opts),
        phase="ontology",
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )
    owl_files = list((REPO_ROOT / "data" / "obo").glob("*.owl"))
    if not owl_files:
        _events.error(
            "ontologies_download_failed",
            phase="ontology",
            reason="no_owl_files",
            path="data/obo",
        )
        raise FileNotFoundError(
            "No OWL files found in data/obo/ after OntologyDownloader"
        )
    _events.info(
        "ontologies_downloaded",
        phase="ontology",
        records_out=len(owl_files),
        duration_ms=_ms(started),
    )


@task(name="slim-ontologies", log_prints=True)
def slim_ontologies(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run OntologySlimmer to filter downloaded OWL files before graph loading.

    OntologySlimmer reduces two ontologies to the permitted taxa (currently
    human only, NCBITaxon:9606; see TaxonConfig.PERMITTED_TAXA):
    ``pr.owl`` -> ``pr-slim.owl`` keeps only protein classes asserted to be in
    a permitted taxon (inclusion), and ``uberon-base.owl`` ->
    ``uberon-human.owl`` keeps all anatomy except classes whose taxon
    constraints -- ``never_in_taxon``, ``only_in_taxon``, or ``in_taxon`` --
    rule out every permitted taxon (exclusion, using the NCBITaxon hierarchy in
    ``taxslim.owl``).  Without this step OntologyGraphBuilder processes the full
    unfiltered ontologies, which are either extremely large and so degrade
    performance, in the case of PR, or include irrelevant terms, in the case of
    UBERON.  Must run after ``download_ontologies`` and before
    ``build_ontology_graph``.

    Taxon references that survive on kept classes are handled downstream by
    OntologyGraphBuilder, which loads a vertex only for the permitted taxa and
    drops taxon-constraint relations, so non-human taxa and those relations do
    not reach the graph.
    """
    _run_logged(
        _java_cmd("gov.nih.nlm.OntologySlimmer", arango_db_password, java_opts),
        phase="ontology",
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )


@task(name="build-ontology-graph", log_prints=True)
def build_ontology_graph(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run OntologyGraphBuilder to load OWL triples into ArangoDB."""
    _run_logged(
        _java_cmd("gov.nih.nlm.OntologyGraphBuilder", arango_db_password, java_opts),
        phase="ontology",
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )


@task(name="dump-arangodb", log_prints=True)
def dump_arangodb(
    dump_dir: Path,
    arango_db_password: str,
    label: str = "",
) -> None:
    """Snapshot the ArangoDB database to a local directory via ``arangodump``.

    Runs ``arangodump`` inside the ArangoDB Docker container (where the binary
    is available) and copies the resulting files to ``dump_dir`` on the host
    via ``docker cp`` (``get_archive``).

    Parameters
    ----------
    dump_dir:
        Destination directory for the dump files.  Created if it does not
        exist.  The directory name should reflect the dump's purpose (e.g.
        ``data/arangodump-baseline-full/``).
    arango_db_password:
        ArangoDB root password.
    label:
        Value of the ``dump_label`` log field (e.g. ``"baseline"``); defaults
        to the dump directory name.
    """
    started = time.monotonic()
    dump_dir = Path(dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_label = label or dump_dir.name

    container_out = f"/tmp/{dump_dir.name}"
    client = docker_sdk.from_env()
    container = client.containers.get(_get_arangodb_id())
    result = container.exec_run(
        [
            "arangodump",
            "--server.endpoint=tcp://127.0.0.1:8529",
            "--server.username=root",
            f"--server.password={arango_db_password}",
            f"--output-directory={container_out}",
            "--overwrite=true",
            "--all-databases=true",
        ]
    )
    output = result.output.decode(errors="replace")
    if result.exit_code != 0:
        _events.error(
            "arangodump_failed",
            dump_label=dump_label,
            reason="nonzero_exit",
            rc=result.exit_code,
            duration_ms=_ms(started),
            output=output[-_MAX_TOOL_OUTPUT:],
        )
        raise RuntimeError(f"arangodump failed (exit {result.exit_code}):\n{output}")
    _events.debug(
        "arangodump_output", dump_label=dump_label, output=output[-_MAX_TOOL_OUTPUT:]
    )

    stream, _ = container.get_archive(container_out)
    with tempfile.TemporaryFile() as tmp:
        for chunk in stream:
            tmp.write(chunk)
        tmp.seek(0)
        with tarfile.open(fileobj=tmp) as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts
                if len(parts) <= 1:
                    continue
                member.name = str(Path(*parts[1:]))
                tar.extract(member, dump_dir)

    dump_files = [f for f in dump_dir.rglob("*") if f.is_file()]
    _events.info(
        "arangodump_finished",
        dump_label=dump_label,
        file_count=len(dump_files),
        bytes=sum(f.stat().st_size for f in dump_files),
        rc=result.exit_code,
        duration_ms=_ms(started),
    )


@task(name="export-graphs-and-analyzers", log_prints=True)
def export_graphs_and_analyzers(
    dump_dir: Path,
    arango_db_password: str,
) -> None:
    """Export named graph definitions and custom analyzers as sidecar files.

    Writes two files per database into ``dump_dir/<db>/``:

    - ``ckn-graphs.ndjson``    — graph objects from ``GET /_db/<db>/_api/gharial``
    - ``ckn-analyzers.ndjson`` — user-defined analyzers from ``GET /_db/<db>/_api/analyzer``
      (only those whose name contains ``::``).

    The ``.ndjson`` extension is intentionally non-standard so that
    ``arangorestore`` ignores these files during restore.  The UI deploy
    pipeline (``deploy-dataset.sh``) reads them after ``arangorestore``
    completes and recreates the graphs and analyzers via the REST API,
    avoiding any dependency on ``--include-system-collections``.

    Parameters
    ----------
    dump_dir:
        Dump directory produced by ``dump_arangodb``.
    arango_db_password:
        ArangoDB root password.

    Raises
    ------
    RuntimeError
        If any database's graphs or analyzers could not be exported.  A dump
        without its sidecars restores to a database missing its named graphs
        (starving the induced subgraph of ontology edges) and analyzers, so the
        failure must stop the run before the dump is cached or promoted.  Every
        database is attempted before raising so all failures are logged.
    """
    import base64
    import urllib.request

    dump_dir = Path(dump_dir)
    failures: list[str] = []

    auth = base64.b64encode(f"root:{arango_db_password}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    base_url = f"http://{ARANGO_DB_HOST}:{ARANGO_DB_PORT}"

    _ARANGO_TIMEOUT = 30  # seconds; guards against a hung ArangoDB during export

    def _get(path: str) -> dict:
        req = urllib.request.Request(f"{base_url}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=_ARANGO_TIMEOUT) as resp:
            return json.loads(resp.read())

    databases = [
        d.name for d in dump_dir.iterdir() if d.is_dir() and not d.name.startswith("_")
    ]

    for db in sorted(databases):
        db_dir = dump_dir / db
        graph_count = analyzer_count = None

        try:
            data = _get(f"/_db/{db}/_api/gharial")
            graphs = data.get("graphs", [])
            # True NDJSON: one compact object per line (empty list → empty file).
            # The deploy consumer iterates a stream of JSON objects; a single
            # pretty-printed array would be read as one value and break on `.name`.
            (db_dir / "ckn-graphs.ndjson").write_text(
                "".join(json.dumps(g) + "\n" for g in graphs)
            )
            graph_count = len(graphs)
        except Exception as exc:
            _events.error(
                "graph_sidecar_export_failed",
                database=db,
                kind="graphs",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            failures.append(f"graphs for {db}")

        try:
            data = _get(f"/_db/{db}/_api/analyzer")
            analyzers = [a for a in data.get("result", []) if "::" in a.get("name", "")]
            # True NDJSON: one compact object per line (empty list → empty file).
            (db_dir / "ckn-analyzers.ndjson").write_text(
                "".join(json.dumps(a) + "\n" for a in analyzers)
            )
            analyzer_count = len(analyzers)
        except Exception as exc:
            _events.error(
                "graph_sidecar_export_failed",
                database=db,
                kind="analyzers",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            failures.append(f"analyzers for {db}")

        if graph_count is not None and analyzer_count is not None:
            _events.info(
                "graph_sidecars_exported",
                database=db,
                graph_count=graph_count,
                analyzer_count=analyzer_count,
            )

    if failures:
        raise RuntimeError(f"Could not export {', '.join(failures)}")


@task(name="import-graphs-from-sidecar", log_prints=True)
def import_graphs_from_sidecar(dump_dir: Path, arango_db_password: str) -> None:
    """Recreate named graphs from ``ckn-graphs.ndjson`` sidecars after a restore.

    ``arangodump`` excludes the ``_graphs`` system collection, so named graph
    definitions do not survive dump/restore.  ``export_graphs_and_analyzers``
    writes them next to the dump; this task reads them back and recreates any
    graph missing on the target (via the gharial REST API) so downstream steps
    such as ``InducedSubgraphBuilder`` can load the graph by name.  The edge
    collections the definitions reference are ordinary collections and are
    already present from the restore.

    Parameters
    ----------
    dump_dir:
        Dump directory whose ``<db>/ckn-graphs.ndjson`` sidecars were written
        by ``export_graphs_and_analyzers``.
    arango_db_password:
        ArangoDB root password.

    Raises
    ------
    RuntimeError
        If any graph could not be recreated (an existing graph, HTTP 409, is
        not a failure).  Continuing would build the results graph without the
        ontology edge definitions.  Every graph is attempted before raising.
    """
    import base64
    import urllib.error
    import urllib.request

    dump_dir = Path(dump_dir)
    failures: list[str] = []

    auth = base64.b64encode(f"root:{arango_db_password}".encode()).decode()
    base_url = f"http://{ARANGO_DB_HOST}:{ARANGO_DB_PORT}"
    _ARANGO_TIMEOUT = 30  # seconds

    db_dirs = sorted(
        p for p in dump_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
    )
    for db_dir in db_dirs:
        db = db_dir.name
        sidecar = db_dir / "ckn-graphs.ndjson"
        if not sidecar.exists():
            continue
        for line in sidecar.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            graph = json.loads(line)
            name = graph.get("name")
            if not name:
                continue
            body = json.dumps(
                {
                    "name": name,
                    "edgeDefinitions": graph.get("edgeDefinitions", []),
                    "orphanCollections": graph.get("orphanCollections", []),
                }
            ).encode()
            req = urllib.request.Request(
                f"{base_url}/_db/{db}/_api/gharial",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=_ARANGO_TIMEOUT) as resp:
                    resp.read()
                _events.info("graph_recreated", database=db, graph=name)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    _events.info("graph_already_present", database=db, graph=name)
                else:
                    detail = exc.read().decode(errors="ignore")
                    _events.error(
                        "graph_recreate_failed",
                        database=db,
                        graph=name,
                        reason="http_error",
                        status_code=exc.code,
                        error=detail[:500],
                    )
                    failures.append(f"{db}/{name}")
    if failures:
        raise RuntimeError(f"Could not recreate graph(s): {', '.join(failures)}")


@task(name="restore-arangodb", log_prints=True)
def restore_arangodb(
    dump_dir: Path,
    arango_db_password: str,
) -> None:
    """Restore ArangoDB from a dump directory created by ``dump_arangodb``.

    Copies ``dump_dir`` into the ArangoDB Docker container via ``docker cp``
    (``put_archive``) then runs ``arangorestore`` inside the container.

    Parameters
    ----------
    dump_dir:
        Source directory containing the dump files (written by
        ``arangodump``).  Raises ``FileNotFoundError`` if it does not exist.
    arango_db_password:
        ArangoDB root password.
    """
    started = time.monotonic()
    dump_dir = Path(dump_dir)
    if not dump_dir.is_dir():
        raise FileNotFoundError(
            f"Baseline dump not found: {dump_dir}\n"
            "Run the ontology stage first (--run-ontology) to create it."
        )

    container_in = f"/tmp/{dump_dir.name}"
    client = docker_sdk.from_env()
    container = client.containers.get(_get_arangodb_id())

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(dump_dir, arcname=dump_dir.name)
    buf.seek(0)
    container.put_archive("/tmp", buf.getvalue())

    result = container.exec_run(
        [
            "arangorestore",
            "--server.endpoint=tcp://127.0.0.1:8529",
            "--server.username=root",
            f"--server.password={arango_db_password}",
            f"--input-directory={container_in}",
            "--overwrite=true",
            "--all-databases=true",
            # Create any database in the dump that is missing on the target.
            # Required because each phase restores into a freshly-wiped instance
            # where only _system exists; without this arangorestore refuses with
            # "database does not exist".
            "--create-database=true",
        ]
    )
    output = result.output.decode(errors="replace")
    if result.exit_code != 0:
        _events.error(
            "arangorestore_failed",
            dump_label=dump_dir.name,
            reason="nonzero_exit",
            rc=result.exit_code,
            duration_ms=_ms(started),
            output=output[-_MAX_TOOL_OUTPUT:],
        )
        raise RuntimeError(f"arangorestore failed (exit {result.exit_code}):\n{output}")
    _events.debug(
        "arangorestore_output",
        dump_label=dump_dir.name,
        output=output[-_MAX_TOOL_OUTPUT:],
    )
    error_lines = [line for line in output.splitlines() if _ERROR_LINE.search(line)]
    if error_lines:
        # arangorestore can report errors and still exit 0.
        _events.warning(
            "arangorestore_reported_errors",
            dump_label=dump_dir.name,
            error_lines=len(error_lines),
            sample=error_lines[:3],
        )
    _events.info(
        "arangorestore_finished",
        dump_label=dump_dir.name,
        rc=result.exit_code,
        duration_ms=_ms(started),
    )


@task(name="validate-release-dir", log_prints=True)
def validate_release_dir(run_name: str = "") -> None:
    """Fail fast if the flat release directory is missing or empty.

    Checks that ``data/results-<name>/`` exists, contains at least one
    ``results_ensg_*.csv`` file (NSForest output), and contains
    ``hubmap_urls.txt``.  This directory is populated by
    ``extract_release_zip`` in the release flow.

    Parameters
    ----------
    run_name:
        Run name (selects ``data/results-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    run_name = run_name or os.getenv("CKN_RUN", "full")
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"

    if not results_dir.is_dir():
        raise FileNotFoundError(
            f"Release directory not found: {results_dir.name}/\n"
            "Extract the nlm-ckn release zip first:\n"
            f"  unzip release-{run_name}.zip -d data/results-{run_name}/"
        )

    nsforest_files = sorted(results_dir.glob("results_ensg_*.csv"))
    if not nsforest_files:
        raise FileNotFoundError(
            f"{results_dir.name}/ contains no results_ensg_*.csv files.\n"
            "The release zip may be empty or incorrectly extracted."
        )

    hubmap_path = results_dir / "hubmap_urls.txt"
    if not hubmap_path.exists():
        raise FileNotFoundError(
            f"{results_dir.name}/hubmap_urls.txt not found.\n"
            "The release zip is missing the HuBMAP URL list."
        )

    _events.info("release_dir_validated", records_out=len(nsforest_files))


@task(name="sync-results-from-s3", log_prints=True)
def sync_results_from_s3(run_name: str = "") -> None:
    """Pull the flat release directory from S3 into ``data/results-<name>/``.

    The release zip is extracted to ``data/results-<name>/`` by the release
    flow.  When running the pipeline standalone (without the release flow),
    this task restores that directory from S3 if ``S3_BUCKET`` is set.

    S3 layout mirrors the local path::

        s3://bucket/results-<name>/ → data/results-<name>/

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name.  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    if not S3_BUCKET:
        return
    run_name = run_name or os.getenv("CKN_RUN", "full")
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"
    results_dir.mkdir(parents=True, exist_ok=True)
    s3_src = f"s3://{S3_BUCKET}/runs/{run_name}/01-results/"
    _s3_sync(s3_src, str(results_dir), artifact="release_results")


@task(name="write-tuples", log_prints=True)
def write_tuples(arango_db_password: str, run_name: str = "") -> None:
    """Run TupleWriterPipeline.py to write all tuple types in sequence.

    Delegates to the unified pipeline script which runs each writer in order:
    NSForest, Mapping, CELLxGENE, Open Targets, Gene, UniProt.

    Parameters
    ----------
    run_name:
        Run name passed as ``--run-name`` (selects ``data/results-<name>/``).
        Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    extra_args = ["--run-name", run_name] if run_name else None
    _run_python_script(
        "TupleWriterPipeline.py",
        arango_db_password,
        extra_args=extra_args,
        phase="results",
    )


@task(name="sync-tuples-to-s3", log_prints=True)
def sync_tuples_to_s3(run_name: str = "") -> None:
    """Push tuple JSON files from ``data/tuples-<name>/`` to S3.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name (selects ``data/tuples-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    if not S3_BUCKET:
        return
    run_name = run_name or os.getenv("CKN_RUN", "full")
    tuples_dir = REPO_ROOT / "data" / f"tuples-{run_name}"
    s3_dest = f"s3://{S3_BUCKET}/runs/{run_name}/05-tuples.tar.gz"
    _s3_upload_tar(tuples_dir, s3_dest, artifact="tuples")


@task(name="validate-tuple-files", log_prints=True)
def validate_tuple_files(run_name: str = "") -> None:
    """Raise an error if no JSON files were produced in ``data/tuples-<name>/``.

    Parameters
    ----------
    run_name:
        Run name (selects ``data/tuples-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    run_name = run_name or os.getenv("CKN_RUN", "full")
    tuples_dir = REPO_ROOT / "data" / f"tuples-{run_name}"
    json_files = sorted(tuples_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(
            f"No JSON files in {tuples_dir.name}/ after tuple writers ran.\n"
            f"Ensure data/results-{run_name}/ contains NSForest results (results_ensg_*.csv)."
        )
    sizes = {f.name: f.stat().st_size for f in json_files}
    for name, size in sizes.items():
        # An empty file or an empty JSON list: a writer ran but produced no rows.
        if size <= 2:
            _events.warning("tuple_file_empty", file=name, bytes=size)
    _events.info(
        "tuple_files_validated", records_out=len(json_files), bytes=sum(sizes.values())
    )


@task(name="sync-baseline-dump-to-s3", log_prints=True)
def sync_baseline_dump_to_s3(baseline_dump_dir: Path, jar_key: str) -> None:
    """Push the Phase 1 baseline dump to ``baselines/{jar_key}/`` in S3.

    Keying by JAR content hash ties the dump to the exact JAR that produced
    it, so Phase 2 always restores a dump that is compatible with the JAR
    currently in use.  No-op when ``S3_BUCKET`` is empty.
    """
    if not S3_BUCKET:
        return
    s3_dest = f"s3://{S3_BUCKET}/baselines/{jar_key}/baseline-dump.tar.gz"
    _s3_upload_tar(baseline_dump_dir, s3_dest, artifact="baseline_dump")


@task(name="sync-baseline-dump-from-s3", log_prints=True)
def sync_baseline_dump_from_s3(baseline_dump_dir: Path, jar_key: str) -> None:
    """Restore the Phase 1 baseline dump from S3 if it is not present locally.

    Looks up the dump under ``baselines/{jar_key}/``, so it fetches the dump
    that matches the JAR currently in use.  No-op when ``S3_BUCKET`` is empty
    or when the dump already exists locally.
    """
    if not S3_BUCKET:
        return
    if Path(baseline_dump_dir).is_dir():
        _events.info(
            "artifact_cache_hit",
            artifact="baseline_dump",
            path=Path(baseline_dump_dir).name,
            jar_key=jar_key,
        )
        return
    s3_src = f"s3://{S3_BUCKET}/baselines/{jar_key}/baseline-dump.tar.gz"
    _s3_download_tar(s3_src, Path(baseline_dump_dir), artifact="baseline_dump")


@task(name="sync-results-dump-to-s3", log_prints=True)
def sync_results_dump_to_s3(
    results_dump_dir: Path, jar_key: str, run_name: str
) -> None:
    """Push the Phase 2 results dump to ``results/{jar_key}/{run_name}/`` in S3.

    Keyed by JAR content hash *and* run name because the results state depends
    on both the JAR (which built the ontology baseline) and the run's input
    tuples.  Phase 3 restores this dump when archiving standalone.  No-op when
    ``S3_BUCKET`` is empty.
    """
    if not S3_BUCKET:
        return
    s3_dest = f"s3://{S3_BUCKET}/results/{jar_key}/{run_name}/results-dump.tar.gz"
    _s3_upload_tar(results_dump_dir, s3_dest, artifact="results_dump")


@task(name="sync-results-dump-from-s3", log_prints=True)
def sync_results_dump_from_s3(
    results_dump_dir: Path, jar_key: str, run_name: str
) -> None:
    """Restore the Phase 2 results dump from S3 if it is not present locally.

    Looks up the dump under ``results/{jar_key}/{run_name}/`` so it fetches the
    dump matching the JAR and run currently in use.  No-op when ``S3_BUCKET`` is
    empty or when the dump already exists locally.
    """
    if not S3_BUCKET:
        return
    if Path(results_dump_dir).is_dir():
        _events.info(
            "artifact_cache_hit",
            artifact="results_dump",
            path=Path(results_dump_dir).name,
            jar_key=jar_key,
            run_name=run_name,
        )
        return
    s3_src = f"s3://{S3_BUCKET}/results/{jar_key}/{run_name}/results-dump.tar.gz"
    _s3_download_tar(s3_src, Path(results_dump_dir), artifact="results_dump")


@task(name="build-results-graph", log_prints=True)
def build_results_graph(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
    run_name: str = "",
) -> None:
    """Run ResultsGraphBuilder to load result tuples into ArangoDB."""
    run_name = run_name or os.getenv("CKN_RUN", "full")
    _run_logged(
        _java_cmd("gov.nih.nlm.ResultsGraphBuilder", arango_db_password, java_opts),
        phase="results",
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password), "CKN_RUN": run_name},
    )


@task(name="build-induced-subgraph", log_prints=True)
def build_induced_subgraph(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run InducedSubgraphBuilder to construct the induced subgraph in ArangoDB.

    Must run after ``build_results_graph`` and before ``create_analyzers_and_views``.
    """
    _run_logged(
        _java_cmd("gov.nih.nlm.InducedSubgraphBuilder", arango_db_password, java_opts),
        phase="results",
        cwd=REPO_ROOT,
        env={
            **os.environ,
            **_arango_env(arango_db_password),
            "ARANGO_ONTOLOGY_DB_NAME": "Cell-KN-Ontologies",
            "ARANGO_ONTOLOGY_GRAPH_NAME": "KN-Ontologies-v2.0",
            "ARANGO_PHENOTYPE_DB_NAME": "Cell-KN-Phenotypes",
            "ARANGO_PHENOTYPE_GRAPH_NAME": "KN-Phenotypes-v2.0",
        },
    )


@task(name="create-analyzers-and-views", log_prints=True)
def create_analyzers_and_views(arango_db_password: str, database: str) -> None:
    """Create ArangoDB analyzers and a search view in the named database.

    Runs in-process via ``ArangoDbUtilities`` (which connects lazily from the
    environment) rather than shelling out.  Called once per phase for the
    database that phase populates: ``Cell-KN-Ontologies`` after the results
    graph is built (Phase 2) and ``Cell-KN-Phenotypes`` after the induced
    subgraph is built (Phase 3).  Deletion of any pre-existing view/analyzers
    is best-effort so a freshly restored database is handled cleanly.
    """
    started = time.monotonic()

    # ArangoDbUtilities reads connection settings from the environment at call
    # time; export the live host/port (the port is assigned dynamically at
    # container start) and password so it connects to this run's instance.
    os.environ["ARANGO_DB_HOST"] = ARANGO_DB_HOST
    os.environ["ARANGO_DB_PORT"] = str(ARANGO_DB_PORT)
    os.environ["ARANGO_DB_PASSWORD"] = arango_db_password

    import ArangoDbUtilities as adb

    collection_maps = REPO_ROOT / "data" / "nlm-ckn-collection-maps.json"
    for delete_fn in (adb.delete_view, adb.delete_analyzers):
        try:
            delete_fn(database)
        except Exception as exc:
            # Usually nothing to delete.  Any other cause (network, auth) shows
            # again, as an error, when the create calls below fail.
            _events.debug(
                "analyzers_delete_skipped",
                database=database,
                step=delete_fn.__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
    adb.create_analyzers(database)
    adb.create_view(database, collection_maps_name=collection_maps)
    _events.info(
        "analyzers_and_views_created", database=database, duration_ms=_ms(started)
    )


@task(name="promote-to-production", log_prints=True)
def promote_to_production(
    golden_dump_dir: Path,
    arango_db_password: str,
    jar_key: str = "",
    run_name: str = "",
) -> None:
    """Upload the golden ArangoDB dump and supporting artifacts to production S3.

    Syncs the following to ``s3://${S3_BUCKET}/runs/<name>/``:

    - ``06-golden-dump/`` — the golden database dump (from ``golden_dump_dir``)
    - ``03-obo/``         — downloaded OWL ontology files
    - ``02-external/``    — snapshot of the external API cache for this run
    - ``build-info.txt``  — version metadata including fetch timestamp and jar_key

    The baseline dump is stored separately under ``s3://${S3_BUCKET}/baselines/<jar_key>/``
    and is NOT re-uploaded here — it was already pushed at the end of Phase 1.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    golden_dump_dir:
        Path to the golden dump directory created by ``dump_arangodb``.
    arango_db_password:
        ArangoDB root password (unused here; kept for consistency).
    jar_key:
        16-char JAR content hash (from ``ensure_jar``).  Recorded in
        ``build-info.txt`` so a run can be traced back to its baseline dump.
    run_name:
        Run name (used to locate ``data/external-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    if not S3_BUCKET:
        return

    started = time.monotonic()
    golden_dump_dir = Path(golden_dump_dir)
    if not golden_dump_dir.is_dir():
        raise FileNotFoundError(
            f"Golden dump not found: {golden_dump_dir}\n"
            "Run the archive stage first (--run-archive)."
        )

    run_name = run_name or os.getenv("CKN_RUN", "full")
    run_prefix = f"s3://{S3_BUCKET}/runs/{run_name}"
    objects = 0
    total_bytes = 0

    # Upload golden dump (06-golden-dump.tar.gz)
    s3_golden = f"{run_prefix}/06-golden-dump.tar.gz"
    stats = _s3_upload_tar(golden_dump_dir, s3_golden, artifact="golden")
    objects += 1
    total_bytes += stats["bytes"]

    # Snapshot OBO files into the run (03-obo.tar.gz)
    obo_dir = REPO_ROOT / "data" / "obo"
    if obo_dir.is_dir():
        s3_obo = f"{run_prefix}/03-obo.tar.gz"
        stats = _s3_upload_tar(obo_dir, s3_obo, artifact="obo")
        objects += 1
        total_bytes += stats["bytes"]
    else:
        # Log and continue: the production promotion is incomplete without it.
        _events.warning(
            "artifact_upload_skipped", artifact="obo", reason="missing_directory"
        )

    # Snapshot external API cache into the run (02-external.tar.gz).
    # The shared warm cache at s3://bucket/external/ is the source of truth for
    # incremental fetches; this copy locks the exact state used for this run.
    ext_dir = _external_dir(run_name)
    if ext_dir.is_dir():
        s3_ext = f"{run_prefix}/02-external.tar.gz"
        stats = _s3_upload_tar(ext_dir, s3_ext, artifact="external")
        objects += 1
        total_bytes += stats["bytes"]
    else:
        _events.warning(
            "artifact_upload_skipped", artifact="external", reason="missing_directory"
        )

    # Build and upload build-info.txt
    commit = _git_commit()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    build_info_lines = [
        f"Built at: {timestamp}",
        f"Commit:   {commit}",
        f"Run:      {run_name}",
        f"JAR key:  {jar_key or 'unknown'}",
    ]

    gh_server = os.getenv("GITHUB_SERVER_URL")
    gh_repo = os.getenv("GITHUB_REPOSITORY")
    gh_run_id = os.getenv("GITHUB_RUN_ID")
    gh_workflow = os.getenv("GITHUB_WORKFLOW")
    gh_actor = os.getenv("GITHUB_ACTOR")
    if gh_server and gh_repo and gh_run_id:
        build_info_lines += [
            f"GHA workflow: {gh_workflow or 'unknown'}",
            f"GHA actor:    {gh_actor or 'unknown'}",
            f"GHA run:      {gh_server}/{gh_repo}/actions/runs/{gh_run_id}",
        ]

    fetch_info_path = ext_dir / "fetch-info.json"
    if fetch_info_path.exists():
        try:
            fetch_info = json.loads(fetch_info_path.read_text())
            build_info_lines += [
                f"Fetched at:   {fetch_info.get('fetched_at', 'unknown')}",
                f"Fetch commit: {fetch_info.get('commit', 'unknown')}",
            ]
            for fname, size in fetch_info.get("files", {}).items():
                size_str = f"{size:,} bytes" if size is not None else "missing"
                build_info_lines.append(f"  {fname}: {size_str}")
        except Exception as exc:
            # Log and continue: build-info.txt then loses the fetch provenance.
            _events.warning(
                "fetch_info_unreadable",
                source="local",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    try:
        pom = (REPO_ROOT / "pom.xml").read_text()
        java_version = re.search(r"<version>([^<]+)</version>", pom).group(1).strip()
        build_info_lines.append(f"Java version: {java_version}")
    except Exception as exc:
        _events.warning(
            "build_version_unavailable",
            component="java",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    try:
        pyproject = (REPO_ROOT / "python" / "pyproject.toml").read_text()
        py_version = (
            re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
            .group(1)
            .strip()
        )
        build_info_lines.append(f"Python version: {py_version}")
    except Exception as exc:
        _events.warning(
            "build_version_unavailable",
            component="python",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    build_info_path = REPO_ROOT / "build-info.txt"
    build_info_path.write_text("\n".join(build_info_lines) + "\n")
    if S3_BUCKET:
        bucket, key = _parse_s3_url(f"{run_prefix}/build-info.txt")
        sse_args = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": S3_KMS_KEY_ID}
            if S3_KMS_KEY_ID
            else {"ServerSideEncryption": "AES256"}
        )
        boto3.client("s3").upload_file(
            str(build_info_path),
            bucket,
            key,
            ExtraArgs={"ACL": "private", **sse_args},
        )
        objects += 1
        total_bytes += build_info_path.stat().st_size

    _events.info(
        "production_promotion_finished",
        run_name=run_name,
        objects=objects,
        bytes=total_bytes,
        duration_ms=_ms(started),
    )


# ── Flow ───────────────────────────────────────────────────────────────────


def _set_arango_port(port: int) -> None:
    """Update the ArangoDB port in both module namespaces after a dynamic bind.

    start_arangodb picks a free OS port at container-start time.  All tasks
    that build endpoint URLs or env-var dicts read ARANGO_DB_PORT from their
    module globals, so we update both the pipeline module and _common so every
    downstream task sees the actual port.
    """
    globals()["ARANGO_DB_PORT"] = port
    _common_mod.ARANGO_DB_PORT = port


def _run_etl(
    run_ontology: bool,
    force_ontology: bool,
    run_results: bool,
    force_results: bool,
    run_archive: bool,
    force_archive: bool,
    java_opts: str,
    run_name: str,
    summary: dict,
) -> None:
    """Run the phases of :func:`nlm_ckn_etl`, recording them in ``summary``.

    Logs ``config_resolved`` once, then ``phase_started`` and ``phase_finished``
    (or ``phase_skipped``) for each phase.  ``summary`` receives the ``phase`` in
    progress, the finished phases' ``durations`` and the ``skipped`` phases.
    """
    if not any(
        [
            run_ontology,
            force_ontology,
            run_results,
            force_results,
            run_archive,
            force_archive,
        ]
    ):
        raise ValueError(
            "No stage flags set — nothing to do.  Pass at least one of: "
            "run_ontology, force_ontology, run_results, force_results, "
            "run_archive, force_archive."
        )

    arango_db_password = _get_or_create_arango_password()
    arango_db_home = ARANGO_DB_HOME
    run_name = run_name or os.getenv("CKN_RUN", "full")

    # Resolve the JAR and derive its content key upfront.  The key is used to
    # locate the matching baseline dump in S3 — ensuring the dump and the JAR
    # that produced it are always stored and retrieved together.
    jar_key = ensure_jar()

    _events.info(
        "config_resolved",
        run_name=run_name,
        s3_enabled=bool(S3_BUCKET),
        arango_local=ARANGO_DB_IS_LOCAL,
        run_ontology=run_ontology,
        force_ontology=force_ontology,
        run_results=run_results,
        force_results=force_results,
        run_archive=run_archive,
        force_archive=force_archive,
        java_opts=java_opts,
        jar_key=jar_key,
        git_commit=_git_commit(),
        fetch_code_hash=_fetch_code_hash(),
        flow_run_id=_common_mod.flow_run.id,
        **_fields(batch_job_id=os.getenv("AWS_BATCH_JOB_ID") or None),
    )

    # Baseline dump: keyed by JAR content hash so the dump and the JAR that
    # produced it are always associated.  Written at the end of Phase 1;
    # restored at the start of Phase 2.
    baseline_dump_dir = REPO_ROOT / "data" / f"arangodump-baseline-{jar_key}"
    # Results dump: ontology + results state, keyed by JAR hash and run name.
    # Written at the end of Phase 2; restored at the start of Phase 3 when
    # archiving standalone so each phase has its own restorable save point.
    results_dump_dir = REPO_ROOT / "data" / f"arangodump-results-{jar_key}-{run_name}"
    # Golden dump: written near the end of Phase 3; uploaded to production S3.
    golden_dump_dir = REPO_ROOT / "data" / f"arangodump-golden-{run_name}"

    # ── Phase 1: Upstream Build ────────────────────────────────────────────
    phase1_started_arangodb = False
    # Whether the live ArangoDB already holds this run's Phase 2 result state
    # (set when Phase 2 runs in this same invocation).  Phase 3 uses it to skip
    # a redundant restore of the results dump.
    phase2_db_is_current = False
    if run_ontology or force_ontology:
        if baseline_dump_dir.is_dir() and not force_ontology:
            # Use force_ontology=True to force a full rebuild.
            summary["skipped"].append("ontology")
            _events.info(
                "phase_skipped",
                phase="ontology",
                reason="dump_exists",
                force=False,
                jar_key=jar_key,
            )
        else:
            with _phase("ontology", summary, jar_key=jar_key) as finish:
                post_github_deployment_status(
                    state="in_progress",
                    description="[3/3] ETL Phase 1: building ontology graph",
                )
                if ARANGO_DB_IS_LOCAL:
                    # Wipe ArangoDB and start fresh so OntologyGraphBuilder has a
                    # clean slate.  The stopped container's data dir is removed so
                    # no stale collections carry over.
                    stop_arangodb()
                    _wipe_arangodb_data(arango_db_home)
                    actual_port = start_arangodb(arango_db_home, arango_db_password)
                    _set_arango_port(actual_port)
                    phase1_started_arangodb = True
                require_arangodb()
                download_ontologies(arango_db_password, java_opts)
                slim_ontologies(arango_db_password, java_opts)
                build_ontology_graph(arango_db_password, java_opts)

                # Save the ontology database state as the baseline dump, keyed by
                # JAR hash.  Phase 2 restores from here, so repeated results runs
                # are cheap and always use a compatible baseline.
                if baseline_dump_dir.is_dir():
                    shutil.rmtree(baseline_dump_dir)
                dump_arangodb(baseline_dump_dir, arango_db_password, label="baseline")
                # Capture the ontology named-graph (and analyzer) definitions next to
                # the dump.  arangodump excludes the _graphs system collection, so
                # without this sidecar the KN-Ontologies-v2.0 edge definitions are
                # lost on restore and Phase 2 would rebuild the graph from results
                # edges only — starving InducedSubgraphBuilder of the ontology edges.
                export_graphs_and_analyzers(baseline_dump_dir, arango_db_password)
                sync_baseline_dump_to_s3(baseline_dump_dir, jar_key)
                finish["dump"] = baseline_dump_dir.name
                finish["dump_bytes"] = _dir_bytes(baseline_dump_dir)

    # Decide which phases actually run.  A phase is skipped when its dump
    # already exists and the matching force flag is not set, so the ArangoDB
    # start/wipe below and the phase body both no-op when there is nothing to
    # rebuild (this is what makes --force-results / --force-archive meaningful).
    run_phase2 = run_results or force_results
    if run_phase2 and results_dump_dir.is_dir() and not force_results:
        summary["skipped"].append("results")
        _events.info(
            "phase_skipped",
            phase="results",
            reason="dump_exists",
            force=False,
            jar_key=jar_key,
            run_name=run_name,
        )
        run_phase2 = False

    run_phase3 = run_archive or force_archive
    if run_phase3 and golden_dump_dir.is_dir() and not force_archive:
        summary["skipped"].append("archive")
        _events.info(
            "phase_skipped",
            phase="archive",
            reason="dump_exists",
            force=False,
            run_name=run_name,
        )
        run_phase3 = False

    # ── Ensure ArangoDB is running for Phase 2/3 when Phase 1 didn't start it ──
    # Covers --run-results and/or --run-archive, and --run-ontology when the
    # baseline already existed and Phase 1 was a no-op.  Archive is included
    # because Phase 3 now restores the results dump into a fresh instance, so
    # archive-only no longer depends on Phase 2 having populated a live database.
    if (
        ARANGO_DB_IS_LOCAL
        and not phase1_started_arangodb
        and (run_phase2 or run_phase3)
    ):
        # Stop and remove any running container BEFORE wiping.  Otherwise the
        # rmtree pulls the bind-mounted data dir out from under a live
        # container (its mount goes stale → ArangoDB can no longer write), and
        # start_arangodb would then reuse that broken container instead of
        # starting fresh.  Mirrors the Phase 1 stop→wipe→start ordering.
        stop_arangodb()
        # Wipe ArangoDB data (bind-mount dir or named Docker volume) before
        # starting so ArangoDB initialises fresh with the current password.
        # Without this, ArangoDB ignores ARANGO_ROOT_PASSWORD on restart and
        # keeps the password baked into the existing data, causing a 401 if
        # the password was regenerated (e.g. in a new Batch container).
        _wipe_arangodb_data(arango_db_home)
        actual_port = start_arangodb(arango_db_home, arango_db_password)
        _set_arango_port(actual_port)

    # ── Phase 2: Iterative Refinement ─────────────────────────────────────
    if run_phase2:
        with _phase("results", summary, jar_key=jar_key, run_name=run_name) as finish:
            post_github_deployment_status(
                state="in_progress",
                description="[3/3] ETL Phase 2: writing tuples and building graphs",
            )

            # Guard: require baseline dump before restoring — pull from S3 if missing locally.
            # Uses jar_key so we always restore the dump produced by this exact JAR.
            sync_baseline_dump_from_s3(baseline_dump_dir, jar_key)
            if not baseline_dump_dir.is_dir():
                raise RuntimeError(
                    f"Baseline dump not found for jar_key={jar_key}: {baseline_dump_dir.name}/\n"
                    "Run Phase 1 first (--run-ontology) to build and dump the "
                    "ontology graph with the current JAR, then re-run with --run-results."
                )

            # Pull inputs from S3 (no-op in local mode)
            sync_results_from_s3(run_name=run_name)  # flat release dir
            sync_external_from_s3(run_name=run_name)  # external API cache from fetcher.py

            validate_release_dir(run_name=run_name)
            validate_external_files(run_name=run_name)
            _log_source_versions(run_name)

            require_arangodb()

            # Restore the baseline so this phase is fully repeatable.
            # Any data written by a previous Phase 2 run is discarded here.
            restore_arangodb(baseline_dump_dir, arango_db_password)

            # Recreate the ontology named graph from the baseline sidecar (the dump
            # excludes _graphs).  Must run before build_results_graph so
            # ResultsGraphBuilder.createOrGetGraph extends the full KN-Ontologies-v2.0
            # definition instead of creating a results-only one.  Whether Phase 1 ran
            # in this invocation or not, the graph definition is now complete.
            import_graphs_from_sidecar(baseline_dump_dir, arango_db_password)

            # Clear the tuples directory before writing fresh output
            tuples_dir = REPO_ROOT / "data" / f"tuples-{run_name}"
            tuples_dir.mkdir(parents=True, exist_ok=True)
            for f in tuples_dir.glob("*.json"):
                f.unlink()

            write_tuples(arango_db_password, run_name=run_name)

            sync_tuples_to_s3(run_name=run_name)  # persist tuple output
            validate_tuple_files(run_name=run_name)
            tuple_files = list(tuples_dir.glob("*.json"))
            finish["tuple_files"] = len(tuple_files)
            finish["tuple_bytes"] = sum(f.stat().st_size for f in tuple_files)

            # Load result tuples into the ontology graph, then create its analyzers
            # and views now that Cell-KN-Ontologies is fully populated.  The induced
            # subgraph is built in Phase 3 from this state.
            build_results_graph(arango_db_password, java_opts, run_name=run_name)
            create_analyzers_and_views(arango_db_password, "Cell-KN-Ontologies")

            # Save the ontology + results state as the results dump, keyed by
            # jar_key + run, so Phase 3 can restore it without re-running Phase 2.
            if results_dump_dir.is_dir():
                shutil.rmtree(results_dump_dir)
            dump_arangodb(results_dump_dir, arango_db_password, label="results")
            # Capture named-graph definitions next to the dump (arangodump excludes
            # the _graphs system collection); Phase 3 recreates them after restoring.
            export_graphs_and_analyzers(results_dump_dir, arango_db_password)
            sync_results_dump_to_s3(results_dump_dir, jar_key, run_name)
            phase2_db_is_current = True
            finish["dump"] = results_dump_dir.name
            finish["dump_bytes"] = _dir_bytes(results_dump_dir)

    # ── Phase 3: Production Handoff ────────────────────────────────────────
    if run_phase3:
        with _phase("archive", summary, jar_key=jar_key, run_name=run_name) as finish:
            post_github_deployment_status(
                state="in_progress",
                description="[3/3] ETL Phase 3: inducing subgraph and promoting to production",
            )
            require_arangodb()

            # When Phase 2 did not run in this invocation, restore the results dump
            # so Phase 3 is self-contained (pull from S3 if missing locally).
            # arangorestore does not reapply analyzers — they live in the _analyzers
            # system collection, which the dump excludes — so recreate the ontology
            # analyzers/views after restoring.
            if not phase2_db_is_current:
                sync_results_dump_from_s3(results_dump_dir, jar_key, run_name)
                if not results_dump_dir.is_dir():
                    raise RuntimeError(
                        f"Results dump not found for jar_key={jar_key}, run={run_name}: "
                        f"{results_dump_dir.name}/\n"
                        "Run Phase 2 first (--run-results) to write tuples and build "
                        "the results graph, then re-run with --run-archive."
                    )
                restore_arangodb(results_dump_dir, arango_db_password)
                # Recreate named graphs the restore dropped (the _graphs system
                # collection is excluded from the dump) so InducedSubgraphBuilder
                # can load KN-Ontologies-v2.0 by name.
                import_graphs_from_sidecar(results_dump_dir, arango_db_password)
                create_analyzers_and_views(arango_db_password, "Cell-KN-Ontologies")

            # Induce the phenotype subgraph from the ontology + results graph, then
            # create its analyzers and views now that Cell-KN-Phenotypes is built.
            build_induced_subgraph(arango_db_password, java_opts)
            create_analyzers_and_views(arango_db_password, "Cell-KN-Phenotypes")

            # Dump the final, fully-built database as the golden artifact.
            if golden_dump_dir.is_dir():
                shutil.rmtree(golden_dump_dir)
            dump_arangodb(golden_dump_dir, arango_db_password, label="golden")
            export_graphs_and_analyzers(golden_dump_dir, arango_db_password)

            # Promote all production artifacts to a versioned S3 path.
            # The baseline dump is NOT re-uploaded here — it lives permanently at
            # s3://bucket/baselines/<jar_key>/ from the end of Phase 1.
            promote_to_production(
                golden_dump_dir, arango_db_password, jar_key=jar_key, run_name=run_name
            )
            finish["dump"] = golden_dump_dir.name
            finish["dump_bytes"] = _dir_bytes(golden_dump_dir)


@flow(name="nlm-ckn-etl", log_prints=True)
def nlm_ckn_etl(
    run_ontology: bool = False,
    force_ontology: bool = False,
    run_results: bool = False,
    force_results: bool = False,
    run_archive: bool = False,
    force_archive: bool = False,
    java_opts: str = DEFAULT_JAVA_OPTS,
    run_name: str = "",
) -> None:
    """NLM-CKN ETL pipeline — three-phase, orchestrated with Prefect.

    **Phase 1 — Upstream Build** (``run_ontology``):
      Download → slim → build ontology graph → ``arangodump`` baseline.
      Skipped automatically when a baseline dump already exists unless
      ``force_ontology=True``.

    **Phase 2 — Iterative Refinement** (``run_results``):
      ``arangorestore`` from baseline → write tuples → build the results graph
      → create ontology analyzers/views → ``arangodump`` the results dump.
      Can be re-run cheaply without repeating Phase 1; skipped when the results
      dump already exists unless ``force_results=True``.  Requires the baseline
      dump to exist.

    **Phase 3 — Production Handoff** (``run_archive``):
      Restore the results dump (when Phase 2 did not just run) → build the
      induced phenotype subgraph → create phenotype analyzers/views →
      ``arangodump`` the golden state → sync all artifacts into
      ``s3://${S3_BUCKET}/runs/<name>/`` (stages 02–06 + build-info.txt).
      Skipped when the golden dump already exists unless ``force_archive=True``.

    At least one stage flag must be ``True``.

    Parameters
    ----------
    run_ontology:
        Run Phase 1 (ontology build + baseline dump) if not already done.
    force_ontology:
        Force a full Phase 1 rebuild, wiping ArangoDB and overwriting any
        existing baseline dump.
    run_results:
        Run Phase 2 (restore baseline → write tuples → build results graph →
        dump results) unless the results dump already exists.
    force_results:
        Run Phase 2 even if its results dump already exists, overwriting it.
    run_archive:
        Run Phase 3 (induce subgraph → golden dump → production S3 promotion)
        unless the golden dump already exists.
    force_archive:
        Run Phase 3 even if its golden dump already exists, overwriting it.
    java_opts:
        JVM flags passed to every Java invocation (default: ``DEFAULT_JAVA_OPTS``,
        currently ``-Xmx32g``).  Increase further if you get OOM-killed (exit 137).
    run_name:
        Run name (selects ``data/results-<name>/`` for tuple writers and
        results sources).  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    started = time.monotonic()
    summary: dict = {"phase": None, "durations": {}, "skipped": []}
    try:
        _run_etl(
            run_ontology,
            force_ontology,
            run_results,
            force_results,
            run_archive,
            force_archive,
            java_opts,
            run_name,
            summary,
        )
    except Exception as exc:
        _events.error(
            "run_failed",
            phase=summary["phase"],
            task=_failing_task(exc),
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=_ms(started),
        )
        raise
    _events.info(
        "run_finished",
        outcome="success",
        duration_ms=_ms(started),
        phases_run=list(summary["durations"]),
        phases_skipped=summary["skipped"],
        phase_durations_ms=summary["durations"],
    )


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    from logging_setup import configure_logging

    configure_logging("etl-pipeline")
    parser = argparse.ArgumentParser(
        description="NLM-CKN ETL pipeline (Prefect) — three-phase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "-o",
        "--run-ontology",
        action="store_true",
        help=(
            "Phase 1: build the ontology graph and save a baseline arangodump. "
            "Skipped automatically when the baseline dump already exists."
        ),
    )
    parser.add_argument(
        "-O",
        "--force-ontology",
        action="store_true",
        help=(
            "Phase 1 (forced): wipe ArangoDB and overwrite the baseline dump "
            "even if one already exists."
        ),
    )
    parser.add_argument(
        "-r",
        "--run-results",
        action="store_true",
        help=(
            "Phase 2: restore the baseline dump, write tuples, build the "
            "results graph, create ontology analyzers/views, and dump the "
            "results. Skipped if the results dump already exists. "
            "Requires the baseline dump produced by --run-ontology."
        ),
    )
    parser.add_argument(
        "-R",
        "--force-results",
        action="store_true",
        help="Phase 2 (forced): run Phase 2 even if its results dump exists.",
    )
    parser.add_argument(
        "-a",
        "--run-archive",
        action="store_true",
        help=(
            "Phase 3: restore the results dump (if Phase 2 did not just run), "
            "build the induced phenotype subgraph, create phenotype "
            "analyzers/views, dump the golden artifact, and promote all "
            "production artifacts to a versioned S3 path. Skipped if the "
            "golden dump already exists."
        ),
    )
    parser.add_argument(
        "-A",
        "--force-archive",
        action="store_true",
        help="Phase 3 (forced): run Phase 3 even if its golden dump exists.",
    )
    parser.add_argument(
        "--java-opts",
        default=DEFAULT_JAVA_OPTS,
        help=(
            f"JVM flags for Java programs (default: '{DEFAULT_JAVA_OPTS}'). "
            "Increase -Xmx if the process is OOM-killed (exit 137)."
        ),
    )
    parser.add_argument(
        "--run-name",
        default=os.getenv("CKN_RUN", ""),
        help=(
            "Run name (selects data/results-<name>/ for tuple writers and results sources). "
            "Defaults to $CKN_RUN or 'full'."
        ),
    )
    args = parser.parse_args()

    nlm_ckn_etl(
        run_ontology=args.run_ontology,
        force_ontology=args.force_ontology,
        run_results=args.run_results,
        force_results=args.force_results,
        run_archive=args.run_archive,
        force_archive=args.force_archive,
        java_opts=args.java_opts,
        run_name=args.run_name,
    )
