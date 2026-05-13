#!/usr/bin/env python3
"""Prefect ETL pipeline for the NLM-CKN project.

Reads external API data (produced by ``fetch.py``) from S3 or the local
filesystem, processes it through tuple writers, loads the results into
ArangoDB, and promotes a golden database dump to production S3.

Three-phase architecture
------------------------
The pipeline is split into three phases that share a common ArangoDB
"baseline dump" as the save point between Phase 1 and Phase 2:

**Phase 1 — Upstream Build** (``--run-ontology``):
  Download OWL ontologies → slim → load into ArangoDB → ``arangodump``
  the resulting database to ``data/arangodump-baseline-<run>/``.  This phase
  is expensive (hours) but only needs to rerun when ontologies change.  Once
  the baseline dump exists, Phase 1 is skipped automatically on subsequent
  invocations unless ``--force-ontology`` is passed.

**Phase 2 — Iterative Refinement** (``--run-results``):
  ``arangorestore`` the baseline dump → write all tuples → build results /
  induced-subgraph graphs → create analyzers and views.  Because
  it restores from the baseline each time, Phase 2 is fully repeatable
  without re-running the expensive ontology build.

**Phase 3 — Production Handoff** (``--run-archive``):
  ``arangodump`` the final database state to
  ``data/arangodump-golden-<run>/``, then sync all production artifacts
  (dump, OBO files, external cache snapshot, build-info) to
  ``s3://${S3_BUCKET}/runs/<run>/`` (stages 02–06 + build-info.txt).

Prerequisites
-------------
Run ``flows/fetch.py`` first (or ensure ``data/external-<run>/`` contains fresh
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
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

import boto3

import docker as docker_sdk
from prefect import flow, get_run_logger, task

import _common as _common_mod
from _common import (
    ARANGO_DB_HOME,
    ARANGO_DB_HOST,
    ARANGO_DB_HOST_HOME,
    ARANGO_DB_PORT,
    ARANGO_DB_VOLUME_NAME,
    CLASSPATH,
    DEFAULT_JAVA_OPTS,
    REPO_ROOT,
    S3_BUCKET,
    _arango_env,
    _external_dir,
    _find_free_port,
    _get_arangodb_id,
    _get_or_create_arango_password,
    _jar_key,
    _run_python_script,
    _s3_cp,
    _s3_download_tar,
    _s3_sync,
    _s3_upload_tar,
    sync_external_from_s3,
    validate_external_files,
)

# ── Tasks ──────────────────────────────────────────────────────────────────


@task(name="stop-arangodb", log_prints=True)
def stop_arangodb() -> None:
    """Stop and remove the running ArangoDB container, if any."""
    logger = get_run_logger()
    try:
        client = docker_sdk.from_env()
    except docker_sdk.errors.DockerException:
        logger.info("Docker daemon unreachable — nothing to stop")
        return
    containers = client.containers.list(filters={"name": "arangodb"}, all=True)
    if not containers:
        logger.info("No ArangoDB container found")
        return
    for container in containers:
        logger.info(
            f"Stopping/removing ArangoDB container {container.short_id} (status={container.status})"
        )
        if container.status == "running":
            container.stop()
        container.remove()


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
    logger = get_run_logger()
    if _get_arangodb_id():
        logger.info("ArangoDB container already running")
        client = docker_sdk.from_env()
        containers = client.containers.list(
            filters={"name": "arangodb", "status": "running"}
        )
        if containers:
            ports = containers[0].ports.get("8529/tcp")
            if ports:
                return int(ports[0]["HostPort"])
        return ARANGO_DB_PORT

    arango_db_port = _find_free_port()
    volume_source, is_named_volume = _arangodb_volume_source(arango_db_home)
    if is_named_volume:
        logger.info(
            f"Starting ArangoDB (named volume={volume_source}, port={arango_db_port})"
        )
        volumes = {volume_source: {"bind": "/var/lib/arangodb3", "mode": "rw"}}
    else:
        Path(volume_source).mkdir(parents=True, exist_ok=True)
        logger.info(f"Starting ArangoDB (home={volume_source}, port={arango_db_port})")
        volumes = {volume_source: {"bind": "/var/lib/arangodb3", "mode": "rw"}}

    client = docker_sdk.from_env()
    client.containers.run(
        "arangodb",
        name="arangodb",
        detach=True,
        environment={"ARANGO_ROOT_PASSWORD": arango_db_password},
        ports={"8529/tcp": arango_db_port},
        volumes=volumes,
    )
    logger.info(f"ArangoDB container started on port {arango_db_port}")
    return arango_db_port


@task(name="require-arangodb", log_prints=True)
def require_arangodb() -> None:
    """Verify ArangoDB is reachable before starting expensive tasks.

    Remote mode (``ARANGO_DB_HOST`` != ``"localhost"``): ArangoDB is
    managed externally (e.g. a dedicated EC2 instance).  Logs the endpoint
    and returns.

    Local mode: raises ``RuntimeError`` if no ArangoDB container is running.
    """
    logger = get_run_logger()
    if ARANGO_DB_HOST != "localhost":
        logger.info(
            f"Remote ArangoDB mode: host={ARANGO_DB_HOST}, port={ARANGO_DB_PORT}"
        )
        return
    cid = _get_arangodb_id()
    if not cid:
        raise RuntimeError(
            "ArangoDB container is not running. "
            "Start it first or run the ontology stage."
        )
    logger.info(f"Local ArangoDB container: {cid}")


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
    logger = get_run_logger()
    jar = REPO_ROOT / CLASSPATH
    if jar.exists():
        logger.info(
            f"JAR already present: {jar.relative_to(REPO_ROOT)} ({jar.stat().st_size:,} bytes)"
        )
        jar_key = _jar_key()
        logger.info(f"JAR key: {jar_key}")
        return jar_key

    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — building JAR locally with Maven")
        subprocess.run(
            ["mvn", "clean", "package", "-DskipTests"],
            check=True,
            cwd=REPO_ROOT,
        )
        if not jar.exists():
            raise FileNotFoundError(f"Maven build succeeded but JAR not found at {jar}")
        jar_key = _jar_key()
        logger.info(
            f"JAR built: {jar.relative_to(REPO_ROOT)} ({jar.stat().st_size:,} bytes), key={jar_key}"
        )
        return jar_key

    jar.parent.mkdir(parents=True, exist_ok=True)
    s3_path = f"s3://{S3_BUCKET}/artifacts/{jar.name}"
    logger.info(f"Downloading JAR from {s3_path}")
    boto3.client("s3").download_file(S3_BUCKET, f"artifacts/{jar.name}", str(jar))
    if not jar.exists():
        raise FileNotFoundError(f"JAR download from {s3_path} failed")
    jar_key = _jar_key()
    logger.info(
        f"JAR downloaded: {jar.relative_to(REPO_ROOT)} ({jar.stat().st_size:,} bytes), key={jar_key}"
    )
    return jar_key


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
        Space-separated JVM flags (e.g. ``"-Xmx4g"``).
    """
    return ["java"] + java_opts.split() + ["-cp", CLASSPATH, main_class]


@task(name="download-ontologies", log_prints=True)
def download_ontologies(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run OntologyDownloader to fetch OWL files into data/obo/."""
    logger = get_run_logger()
    logger.info(f"Downloading ontologies (gov.nih.nlm.OntologyDownloader, {java_opts})")
    subprocess.run(
        _java_cmd("gov.nih.nlm.OntologyDownloader", arango_db_password, java_opts),
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )
    owl_files = list((REPO_ROOT / "data" / "obo").glob("*.owl"))
    if not owl_files:
        raise FileNotFoundError(
            "No OWL files found in data/obo/ after OntologyDownloader"
        )
    logger.info(f"Downloaded {len(owl_files)} OWL file(s) to data/obo/")


@task(name="slim-ontologies", log_prints=True)
def slim_ontologies(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run OntologySlimmer to filter downloaded OWL files before graph loading.

    OntologySlimmer reduces ``pr.owl`` to human-only (NCBITaxon:9606) protein
    classes.  Without this step OntologyGraphBuilder processes the full
    unfiltered PR ontology, which is extremely large and degrades performance.
    Must run after ``download_ontologies`` and before ``build_ontology_graph``.
    """
    logger = get_run_logger()
    logger.info(f"Slimming ontologies (gov.nih.nlm.OntologySlimmer, {java_opts})")
    subprocess.run(
        _java_cmd("gov.nih.nlm.OntologySlimmer", arango_db_password, java_opts),
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )
    logger.info("Ontologies slimmed")


@task(name="build-ontology-graph", log_prints=True)
def build_ontology_graph(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run OntologyGraphBuilder to load OWL triples into ArangoDB."""
    logger = get_run_logger()
    logger.info(
        f"Building ontology graph (gov.nih.nlm.OntologyGraphBuilder, {java_opts})"
    )
    subprocess.run(
        _java_cmd("gov.nih.nlm.OntologyGraphBuilder", arango_db_password, java_opts),
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )
    logger.info("Ontology graph built")


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
        Human-readable label used in log messages (e.g. ``"baseline"``).
    """
    logger = get_run_logger()
    dump_dir = Path(dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    tag = f" ({label})" if label else ""
    logger.info(f"Dumping ArangoDB → {dump_dir.relative_to(REPO_ROOT)}{tag}")

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
    if result.exit_code != 0:
        raise RuntimeError(
            f"arangodump failed (exit {result.exit_code}):\n{result.output.decode()}"
        )

    stream, _ = container.get_archive(container_out)
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue
            member.name = str(Path(*parts[1:]))
            tar.extract(member, dump_dir)

    dump_files = list(dump_dir.glob("*"))
    logger.info(f"Dump complete{tag}: {len(dump_files)} file(s) in {dump_dir.name}/")


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
    logger = get_run_logger()
    dump_dir = Path(dump_dir)
    if not dump_dir.is_dir():
        raise FileNotFoundError(
            f"Baseline dump not found: {dump_dir}\n"
            "Run the ontology stage first (--run-ontology) to create it."
        )
    logger.info(f"Restoring ArangoDB from {dump_dir.relative_to(REPO_ROOT)}")

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
        ]
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"arangorestore failed (exit {result.exit_code}):\n{result.output.decode()}"
        )
    logger.info("ArangoDB restore complete")


@task(name="validate-release-dir", log_prints=True)
def validate_release_dir(run: str = "") -> None:
    """Fail fast if the flat release directory is missing or empty.

    Checks that ``data/results-<run>/`` exists, contains at least one
    ``*_results.csv`` file (NSForest output), and contains
    ``hubmap_urls.txt``.  This directory is populated by
    ``extract_release_zip`` in the release flow.

    Parameters
    ----------
    run:
        Run name (selects ``data/results-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    run_name = run or os.getenv("CKN_RUN", "full")
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"

    if not results_dir.is_dir():
        raise FileNotFoundError(
            f"Release directory not found: {results_dir.name}/\n"
            "Extract the nlm-ckn release zip first:\n"
            f"  unzip release-{run_name}.zip -d data/results-{run_name}/"
        )

    nsforest_files = sorted(results_dir.glob("*_results.csv"))
    if not nsforest_files:
        raise FileNotFoundError(
            f"{results_dir.name}/ contains no *_results.csv files.\n"
            "The release zip may be empty or incorrectly extracted."
        )

    hubmap_path = results_dir / "hubmap_urls.txt"
    if not hubmap_path.exists():
        raise FileNotFoundError(
            f"{results_dir.name}/hubmap_urls.txt not found.\n"
            "The release zip is missing the HuBMAP URL list."
        )

    logger.info(
        f"Release dir OK: {results_dir.name}/ — "
        f"{len(nsforest_files)} NSForest result file(s), hubmap_urls.txt present"
    )


@task(name="sync-results-from-s3", log_prints=True)
def sync_results_from_s3(run: str = "") -> None:
    """Pull the flat release directory from S3 into ``data/results-<run>/``.

    The release zip is extracted to ``data/results-<run>/`` by the release
    flow.  When running the pipeline standalone (without the release flow),
    this task restores that directory from S3 if ``S3_BUCKET`` is set.

    S3 layout mirrors the local path::

        s3://bucket/results-<run>/ → data/results-<run>/

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run:
        Run name.  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping S3 sync (local mode)")
        return
    run_name = run or os.getenv("CKN_RUN", "full")
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"
    results_dir.mkdir(parents=True, exist_ok=True)
    s3_src = f"s3://{S3_BUCKET}/runs/{run_name}/01-results/"
    logger.info(f"Syncing {s3_src} → {results_dir.name}/")
    _s3_sync(s3_src, str(results_dir))
    logger.info("Release dir synced from S3")


@task(name="write-tuples", log_prints=True)
def write_tuples(arango_db_password: str, run: str = "") -> None:
    """Run TupleWriterPipeline.py to write all tuple types in sequence.

    Delegates to the unified pipeline script which runs each writer in order:
    NSForest, Mapping, CELLxGENE, Open Targets, Gene, UniProt, HuBMAP.

    Parameters
    ----------
    run:
        Run name passed as ``--run`` (selects ``data/run-<name>.json``).
        Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    logger.info("Writing all tuples (TupleWriterPipeline)")
    extra_args = ["--run", run] if run else None
    _run_python_script(
        "TupleWriterPipeline.py", arango_db_password, extra_args=extra_args
    )
    logger.info("All tuples written")


@task(name="sync-tuples-to-s3", log_prints=True)
def sync_tuples_to_s3(run: str = "") -> None:
    """Push tuple JSON files from ``data/tuples-<run>/`` to S3.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run:
        Run name (selects ``data/tuples-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping S3 sync (local mode)")
        return
    run_name = run or os.getenv("CKN_RUN", "full")
    tuples_dir = REPO_ROOT / "data" / f"tuples-{run_name}"
    s3_dest = f"s3://{S3_BUCKET}/runs/{run_name}/05-tuples.tar.gz"
    logger.info(f"Compressing and uploading {tuples_dir.name}/ → {s3_dest}")
    _s3_upload_tar(tuples_dir, s3_dest)
    logger.info("Tuples pushed to S3")


@task(name="validate-tuple-files", log_prints=True)
def validate_tuple_files(run: str = "") -> None:
    """Raise an error if no JSON files were produced in ``data/tuples-<run>/``.

    Parameters
    ----------
    run:
        Run name (selects ``data/tuples-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    run_name = run or os.getenv("CKN_RUN", "full")
    tuples_dir = REPO_ROOT / "data" / f"tuples-{run_name}"
    json_files = list(tuples_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(
            f"No JSON files in {tuples_dir.name}/ after tuple writers ran.\n"
            f"Ensure data/run-{run_name}.json points to existing NSForest results."
        )
    logger.info(f"Tuple files: {len(json_files)} JSON file(s) in {tuples_dir.name}/")


@task(name="sync-baseline-dump-to-s3", log_prints=True)
def sync_baseline_dump_to_s3(baseline_dump_dir: Path, jar_key: str) -> None:
    """Push the Phase 1 baseline dump to ``baselines/{jar_key}/`` in S3.

    Keying by JAR content hash ties the dump to the exact JAR that produced
    it, so Phase 2 always restores a dump that is compatible with the JAR
    currently in use.  No-op when ``S3_BUCKET`` is empty.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping baseline dump upload (local mode)")
        return
    s3_dest = f"s3://{S3_BUCKET}/baselines/{jar_key}/baseline-dump.tar.gz"
    logger.info(f"Compressing and uploading baseline dump → {s3_dest}")
    _s3_upload_tar(baseline_dump_dir, s3_dest)
    logger.info(f"Baseline dump uploaded to S3 (jar_key={jar_key})")


@task(name="sync-baseline-dump-from-s3", log_prints=True)
def sync_baseline_dump_from_s3(baseline_dump_dir: Path, jar_key: str) -> None:
    """Restore the Phase 1 baseline dump from S3 if it is not present locally.

    Looks up the dump under ``baselines/{jar_key}/``, so it fetches the dump
    that matches the JAR currently in use.  No-op when ``S3_BUCKET`` is empty
    or when the dump already exists locally.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping baseline dump restore (local mode)")
        return
    if Path(baseline_dump_dir).is_dir():
        logger.info(
            f"Baseline dump already present locally: {Path(baseline_dump_dir).name}/"
        )
        return
    s3_src = f"s3://{S3_BUCKET}/baselines/{jar_key}/baseline-dump.tar.gz"
    logger.info(
        f"Downloading and extracting baseline dump from {s3_src} (jar_key={jar_key})"
    )
    _s3_download_tar(s3_src, Path(baseline_dump_dir))
    logger.info("Baseline dump restored from S3")


@task(name="build-results-graph", log_prints=True)
def build_results_graph(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
    run: str = "",
) -> None:
    """Run ResultsGraphBuilder to load result tuples into ArangoDB."""
    logger = get_run_logger()
    run_name = run or os.getenv("CKN_RUN", "full")
    logger.info(
        f"Building results graph (gov.nih.nlm.ResultsGraphBuilder, {java_opts})"
    )
    subprocess.run(
        _java_cmd("gov.nih.nlm.ResultsGraphBuilder", arango_db_password, java_opts),
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password), "CKN_RUN": run_name},
    )
    logger.info("Results graph built")


@task(name="build-induced-subgraph", log_prints=True)
def build_induced_subgraph(
    arango_db_password: str,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """Run InducedSubgraphBuilder to construct the induced subgraph in ArangoDB.

    Must run after ``build_results_graph`` and before ``create_analyzers_and_views``.
    """
    logger = get_run_logger()
    logger.info(
        f"Building induced subgraph (gov.nih.nlm.InducedSubgraphBuilder, {java_opts})"
    )
    subprocess.run(
        _java_cmd("gov.nih.nlm.InducedSubgraphBuilder", arango_db_password, java_opts),
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, **_arango_env(arango_db_password)},
    )
    logger.info("Induced subgraph built")



@task(name="create-analyzers-and-views", log_prints=True)
def create_analyzers_and_views(arango_db_password: str) -> None:
    """Run CellKnSchemaUtilities.py to create ArangoDB analyzers and search views."""
    logger = get_run_logger()
    logger.info("Creating ArangoDB analyzers and views (CellKnSchemaUtilities)")
    _run_python_script("CellKnSchemaUtilities.py", arango_db_password)
    logger.info("Analyzers and views created")


@task(name="promote-to-production", log_prints=True)
def promote_to_production(
    golden_dump_dir: Path,
    arango_db_password: str,
    jar_key: str = "",
    run: str = "",
) -> None:
    """Upload the golden ArangoDB dump and supporting artifacts to production S3.

    Syncs the following to ``s3://${S3_BUCKET}/runs/<run>/``:

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
    run:
        Run name (used to locate ``data/external-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping production promotion (local mode)")
        return

    golden_dump_dir = Path(golden_dump_dir)
    if not golden_dump_dir.is_dir():
        raise FileNotFoundError(
            f"Golden dump not found: {golden_dump_dir}\n"
            "Run the archive stage first (--run-archive)."
        )

    run_name = run or os.getenv("CKN_RUN", "full")
    run_prefix = f"s3://{S3_BUCKET}/runs/{run_name}"
    logger.info(f"Promoting artifacts to {run_prefix}/")

    # Upload golden dump (06-golden-dump.tar.gz)
    s3_golden = f"{run_prefix}/06-golden-dump.tar.gz"
    logger.info(f"Compressing and uploading golden dump → {s3_golden}")
    _s3_upload_tar(golden_dump_dir, s3_golden)

    # Snapshot OBO files into the run (03-obo.tar.gz)
    obo_dir = REPO_ROOT / "data" / "obo"
    if obo_dir.is_dir():
        s3_obo = f"{run_prefix}/03-obo.tar.gz"
        logger.info(f"Compressing and uploading OBO files → {s3_obo}")
        _s3_upload_tar(obo_dir, s3_obo)
    else:
        logger.warning("data/obo/ not found — skipping OBO upload")

    # Snapshot external API cache into the run (02-external.tar.gz).
    # The shared warm cache at s3://bucket/external/ is the source of truth for
    # incremental fetches; this copy locks the exact state used for this run.
    ext_dir = _external_dir(run)
    if ext_dir.is_dir():
        s3_ext = f"{run_prefix}/02-external.tar.gz"
        logger.info(f"Compressing and snapshotting external cache → {s3_ext}")
        _s3_upload_tar(ext_dir, s3_ext)
    else:
        logger.warning(f"{ext_dir.name}/ not found — skipping external cache snapshot")

    # Build and upload build-info.txt
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        commit = "unknown"

    date_stamp = datetime.now().strftime("%Y-%m-%d")
    build_info_lines = [
        f"Date:    {date_stamp}",
        f"Commit:  {commit}",
        f"Run:     {run_name}",
        f"JAR key: {jar_key or 'unknown'}",
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
            logger.warning(f"Could not read fetch-info.json: {exc}")

    try:
        pom = (REPO_ROOT / "pom.xml").read_text()
        java_version = re.search(r"<version>([^<]+)</version>", pom).group(1).strip()
        build_info_lines.append(f"Java version: {java_version}")
    except Exception:
        pass
    try:
        pyproject = (REPO_ROOT / "python" / "pyproject.toml").read_text()
        py_version = (
            re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
            .group(1)
            .strip()
        )
        build_info_lines.append(f"Python version: {py_version}")
    except Exception:
        pass

    build_info_path = REPO_ROOT / "build-info.txt"
    build_info_path.write_text("\n".join(build_info_lines) + "\n")
    _s3_cp(str(build_info_path), f"{run_prefix}/build-info.txt")

    logger.info(f"All artifacts promoted to {run_prefix}/")


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


@flow(name="nlm-ckn-etl", log_prints=True)
def nlm_ckn_etl(
    run_ontology: bool = False,
    force_ontology: bool = False,
    run_results: bool = False,
    force_results: bool = False,
    run_archive: bool = False,
    force_archive: bool = False,
    java_opts: str = DEFAULT_JAVA_OPTS,
    run: str = "",
) -> None:
    """NLM-CKN ETL pipeline — three-phase, orchestrated with Prefect.

    **Phase 1 — Upstream Build** (``run_ontology``):
      Download → slim → build ontology graph → ``arangodump`` baseline.
      Skipped automatically when a baseline dump already exists unless
      ``force_ontology=True``.

    **Phase 2 — Iterative Refinement** (``run_results``):
      ``arangorestore`` from baseline → write tuples → build results /
<<<<<<< HEAD
      induced-subgraph graphs.  Can be re-run cheaply without
      repeating Phase 1.  Requires the baseline dump to exist.

    **Phase 3 — Production Handoff** (``run_archive``):
      ``arangodump`` the final state → sync all artifacts into
      ``s3://${S3_BUCKET}/runs/<run>/`` (stages 02–06 + build-info.txt).

    At least one stage flag must be ``True``.

    Parameters
    ----------
    run_ontology:
        Run Phase 1 (ontology build + baseline dump) if not already done.
    force_ontology:
        Force a full Phase 1 rebuild, wiping ArangoDB and overwriting any
        existing baseline dump.
    run_results:
        Run Phase 2 (restore baseline → write tuples → build graphs).
    force_results:
        Force Phase 2 even if it has already completed.
    run_archive:
        Run Phase 3 (golden dump + production S3 promotion).
    force_archive:
        Force Phase 3 even if it has already completed.
    java_opts:
        JVM flags passed to every Java invocation (default: ``-Xmx4g``).
        Increase (e.g. ``-Xmx8g``) if you get OOM-killed (exit 137).
    run:
        Run name (selects ``data/run-<name>.json`` for tuple writers and
        results sources).  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()

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
        logger.warning(
            "No stage flags set — nothing to do.  Pass at least one of: "
            "run_ontology, force_ontology, run_results, force_results, "
            "run_archive, force_archive."
        )
        return

    arango_db_password = _get_or_create_arango_password()
    arango_db_home = ARANGO_DB_HOME
    run_name = run or os.getenv("CKN_RUN", "full")

    if S3_BUCKET:
        logger.info(f"S3 mode: bucket={S3_BUCKET}")
    else:
        logger.info("Local mode: S3_BUCKET not set")

    # Resolve the JAR and derive its content key upfront.  The key is used to
    # locate the matching baseline dump in S3 — ensuring the dump and the JAR
    # that produced it are always stored and retrieved together.
    jar_key = ensure_jar()

    # Baseline dump: keyed by JAR content hash so the dump and the JAR that
    # produced it are always associated.  Written at the end of Phase 1;
    # restored at the start of Phase 2.
    baseline_dump_dir = REPO_ROOT / "data" / f"arangodump-baseline-{jar_key}"
    # Golden dump: written at the start of Phase 3; uploaded to production S3.
    golden_dump_dir = REPO_ROOT / "data" / f"arangodump-golden-{run_name}"

    # ── Phase 1: Upstream Build ────────────────────────────────────────────
    phase1_started_arangodb = False
    if run_ontology or force_ontology:
        if baseline_dump_dir.is_dir() and not force_ontology:
            logger.info(
                f"Baseline dump already exists at {baseline_dump_dir.name}/ "
                f"(jar_key={jar_key}); use force_ontology=True to force a full rebuild"
            )
        else:
            logger.info("=== Phase 1: Upstream Build (Ontology) ===")
            if ARANGO_DB_HOST == "localhost":
                # Wipe ArangoDB and start fresh so OntologyGraphBuilder has a
                # clean slate.  The stopped container's data dir is removed so
                # no stale collections carry over.
                stop_arangodb()
                arango_home = Path(arango_db_home)
                if arango_home.exists():
                    shutil.rmtree(arango_home)
                actual_port = start_arangodb(arango_db_home, arango_db_password)
                _set_arango_port(actual_port)
                phase1_started_arangodb = True
            else:
                logger.info(
                    f"Remote ArangoDB at {ARANGO_DB_HOST}:{ARANGO_DB_PORT} — "
                    "skipping container start/stop and data-dir wipe"
                )
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
            sync_baseline_dump_to_s3(baseline_dump_dir, jar_key)
            logger.info(
                f"Phase 1 complete — baseline dump: {baseline_dump_dir.name}/ "
                f"(jar_key={jar_key})"
            )

    # ── Ensure ArangoDB is running for Phase 2/3 when Phase 1 didn't start it ──
    # Covers: --run-results only, --run-archive only, or --run-ontology when the
    # baseline already existed and Phase 1 was a no-op.
    if ARANGO_DB_HOST == "localhost" and not phase1_started_arangodb:
        if run_results or force_results or run_archive or force_archive:
            actual_port = start_arangodb(arango_db_home, arango_db_password)
            _set_arango_port(actual_port)

    # ── Phase 2: Iterative Refinement ─────────────────────────────────────
    if run_results or force_results:
        logger.info("=== Phase 2: Iterative Refinement (Results) ===")

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
        sync_results_from_s3(run=run)  # flat release dir
        sync_external_from_s3(run=run)  # external API cache from fetcher.py

        validate_release_dir(run=run)
        validate_external_files(run=run)

        require_arangodb()

        # Restore the baseline so this phase is fully repeatable.
        # Any data written by a previous Phase 2 run is discarded here.
        restore_arangodb(baseline_dump_dir, arango_db_password)

        # Clear the tuples directory before writing fresh output
        tuples_dir = REPO_ROOT / "data" / f"tuples-{run_name}"
        tuples_dir.mkdir(parents=True, exist_ok=True)
        for f in tuples_dir.glob("*.json"):
            f.unlink()

        write_tuples(arango_db_password, run=run)

        sync_tuples_to_s3(run=run)  # persist tuple output
        validate_tuple_files(run=run)

        build_results_graph(arango_db_password, java_opts, run=run)
        build_induced_subgraph(arango_db_password, java_opts)
        create_analyzers_and_views(arango_db_password)

        logger.info("Phase 2 complete")

    # ── Phase 3: Production Handoff ────────────────────────────────────────
    if run_archive or force_archive:
        logger.info("=== Phase 3: Production Handoff ===")
        require_arangodb()

        # Dump the final, fully-built database as the golden artifact.
        if golden_dump_dir.is_dir():
            shutil.rmtree(golden_dump_dir)
        dump_arangodb(golden_dump_dir, arango_db_password, label="golden")

        # Promote all production artifacts to a versioned S3 path.
        # The baseline dump is NOT re-uploaded here — it lives permanently at
        # s3://bucket/baselines/<jar_key>/ from the end of Phase 1.
        promote_to_production(
            golden_dump_dir, arango_db_password, jar_key=jar_key, run=run
        )

        logger.info("Phase 3 complete")


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
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
            "Phase 2: restore the baseline dump, write tuples, and build "
            "the results / induced-subgraph graphs. "
            "Requires the baseline dump produced by --run-ontology."
        ),
    )
    parser.add_argument(
        "-R",
        "--force-results",
        action="store_true",
        help="Phase 2 (forced): re-run Phase 2 unconditionally.",
    )
    parser.add_argument(
        "-a",
        "--run-archive",
        action="store_true",
        help=(
            "Phase 3: dump the final database state as the golden artifact "
            "and promote all production artifacts to a versioned S3 path."
        ),
    )
    parser.add_argument(
        "-A",
        "--force-archive",
        action="store_true",
        help="Phase 3 (forced): re-run Phase 3 unconditionally.",
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
        "--run",
        default=os.getenv("CKN_RUN", ""),
        help=(
            "Run name (selects data/run-<name>.json for tuple writers and results sources). "
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
        run=args.run,
    )
