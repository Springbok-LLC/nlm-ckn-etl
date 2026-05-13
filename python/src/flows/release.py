#!/usr/bin/env python3
"""Prefect release flow for the NLM-CKN ETL pipeline.

Drives a full end-to-end release from a cell-kn GitHub Release tag:

1. Download the release tarball from GitHub Releases and extract it to
   ``data/results-<run>/`` (preserving the nested organ/dataset structure).
2. Run the external API fetch flow (``nlm_ckn_fetch``) with ``force=True``
   so every release captures a fresh, date-stamped snapshot.
3. Run the three-phase ETL pipeline (``nlm_ckn_etl``):
   - Phase 1: build ontology graph → baseline arangodump
   - Phase 2: restore baseline → write tuples → build graphs
   - Phase 3: golden arangodump → promote to production S3

Release format
--------------
The tarball (``prod-data-<tag>.tar.gz``) contains data under a nested
``data/prod/<organ>/...`` structure.  On extraction all files are flattened
directly into ``data/results-<run>/``, consistent with what
``LoaderUtilities.get_dataset_file_paths`` expects.

HuBMap URLs are no longer bundled in the release archive.  Provide a
local ``hubmap_urls.txt`` file via ``--hubmap-urls-file``; its contents
are written into the results directory so downstream code can find them.

Usage
-----
Run directly::

    python src/flows/release.py --tag v2026-04 --ncbi-email user@example.com --ncbi-api-key KEY

Or via the Prefect CLI::

    prefect deployment run 'nlm-ckn-release/production' \\
        --param cell_kn_tag=v2026-04 \\
        --param ncbi_email=user@example.com \\
        --param ncbi_api_key=KEY

Skip ontology rebuild (reuse existing baseline dump)::

    python src/flows/release.py --tag v2026-04 --skip-ontology ...

GitHub token
------------
Set ``GITHUB_TOKEN`` to authenticate against the GitHub API and avoid rate
limits.  Required for private repositories.
"""

import argparse
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3

from prefect import flow, get_run_logger, task

from _common import (
    DEFAULT_JAVA_OPTS,
    REPO_ROOT,
    S3_BUCKET,
    _external_dir,
    _s3_copy_prefix,
    _s3_sync,
)
from fetch import nlm_ckn_fetch
from pipeline import nlm_ckn_etl

# ── Tasks ──────────────────────────────────────────────────────────────────


_TARBALL_PREFIX = "data/prod/"


@task(name="extract-release-tarball", log_prints=True)
def extract_release_tarball(
    tar_source: str,
    run_name: str,
    hubmap_urls_file: str = "",
) -> Path:
    """Download (or copy) the cell-kn release tarball and extract it.

    The tarball (``prod-data-<tag>.tar.gz``) stores files under a nested
    ``data/prod/<organ>/...`` structure.  On extraction all files are flattened
    directly into ``data/results-<run_name>/``, discarding directory structure.
    Filenames are unique across datasets so no collisions occur.

    HuBMap URLs are no longer bundled in the release.  If ``hubmap_urls_file``
    is provided its contents are written to ``hubmap_urls.txt`` inside the
    results directory so downstream code can find them.

    Parameters
    ----------
    tar_source:
        Either an HTTPS URL (GitHub Release asset) or a local filesystem
        path to the ``.tar.gz`` file.
    run_name:
        Run name used to name the extraction directory
        (``data/results-<run_name>/``).
    hubmap_urls_file:
        Optional path to a local ``hubmap_urls.txt`` file.  When omitted the
        flow looks for ``data/hubmap_urls.txt`` in the repository root.

    Returns
    -------
    Path
        The extracted results directory (``data/results-<run_name>/``).
    """
    logger = get_run_logger()
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"

    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir(parents=True)

    # Download if URL, otherwise use local path directly.
    if tar_source.startswith("http://") or tar_source.startswith("https://"):
        tar_path = REPO_ROOT / "data" / f"release-{run_name}.tar.gz"
        logger.info(f"Downloading release tarball: {tar_source}")
        headers = {}
        token = os.getenv("GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(tar_source, headers=headers)
        with urllib.request.urlopen(req) as resp, open(tar_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        logger.info(f"Downloaded to {tar_path.name}")
    elif tar_source.startswith("s3://"):
        tar_path = REPO_ROOT / "data" / f"release-{run_name}.tar.gz"
        without_scheme = tar_source[len("s3://"):]
        bucket, _, key = without_scheme.partition("/")
        logger.info(f"Downloading s3://{bucket}/{key} → {tar_path.name}")
        boto3.client("s3").download_file(bucket, key, str(tar_path))
        logger.info(f"Downloaded to {tar_path.name}")
    else:
        tar_path = Path(tar_source)
        logger.info(f"Using local tarball: {tar_path}")

    # Extract, flattening all files into results_dir regardless of their nested
    # path within the tarball. Filenames are unique across datasets (they include
    # organ, author, and year) so collisions are not a concern. This keeps
    # results_dir flat, consistent with what LoaderUtilities expects.
    logger.info(f"Extracting → {results_dir.name}/ (flat)")
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.name.startswith(_TARBALL_PREFIX):
                continue
            if member.isdir():
                continue
            member.name = Path(member.name).name
            tf.extract(member, results_dir)

    # Remove downloaded tarball — contents are now in results_dir.
    if tar_path.parent == REPO_ROOT / "data" and tar_path.name.startswith("release-"):
        tar_path.unlink(missing_ok=True)

    # Write hubmap_urls.txt into results_dir.  The source may be a local path
    # or an S3 URL (uploaded by trigger-release.sh before job submission).
    hubmap_dst = results_dir / "hubmap_urls.txt"
    if hubmap_urls_file and hubmap_urls_file.startswith("s3://"):
        without_scheme = hubmap_urls_file[len("s3://"):]
        bucket, _, key = without_scheme.partition("/")
        logger.info(f"Downloading {hubmap_urls_file} → {hubmap_dst.name}")
        boto3.client("s3").download_file(bucket, key, str(hubmap_dst))
    else:
        urls_src = (
            Path(hubmap_urls_file)
            if hubmap_urls_file
            else REPO_ROOT / "data" / "hubmap_urls.txt"
        )
        if not urls_src.exists():
            raise FileNotFoundError(
                f"HuBMap URLs file not found: {urls_src}\n"
                "Provide one via --hubmap-urls-file or place it at data/hubmap_urls.txt."
            )
        shutil.copy(urls_src, hubmap_dst)
    logger.info(f"HuBMap URLs written to {hubmap_dst.name}")

    csv_count = len(list(results_dir.glob("*_results.csv")))
    logger.info(f"Extracted {csv_count} NSForest result files to {results_dir.name}/")
    return results_dir


@task(name="sync-release-dir-to-s3", log_prints=True)
def sync_release_dir_to_s3(run: str = "") -> None:
    """Push ``data/results-<run>/`` to S3 so the pipeline can be re-run
    without re-downloading the zip.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run:
        Run name.  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping (local mode)")
        return
    run_name = run or os.getenv("CKN_RUN", "full")
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"
    s3_dst = f"s3://{S3_BUCKET}/runs/{run_name}/01-results/"
    logger.info(f"Syncing {results_dir.name}/ → {s3_dst}")
    _s3_sync(str(results_dir), s3_dst)
    logger.info("Release dir pushed to S3")


@task(name="promote-results-to-latest", log_prints=True)
def promote_results_to_latest(run: str = "") -> None:
    """Server-side copy ``runs/<run>/01-results/`` → ``runs/latest/01-results/`` in S3.

    Keeps a stable pointer that the scheduled fetch Fargate task always reads
    from, so it uses the most recent release's gene set without requiring a
    CloudFormation redeploy when the run name changes.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run:
        Run name.  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping (local mode)")
        return
    run_name = run or os.getenv("CKN_RUN", "full")
    src_prefix = f"runs/{run_name}/01-results/"
    dst_prefix = "runs/latest/01-results/"
    logger.info(
        f"Promoting s3://{S3_BUCKET}/{src_prefix} → s3://{S3_BUCKET}/{dst_prefix}"
    )
    count = _s3_copy_prefix(S3_BUCKET, src_prefix, dst_prefix)
    logger.info(f"Promoted {count} result file(s) to runs/latest/01-results/")


@task(name="resolve-fetch-force", log_prints=True)
def resolve_fetch_force(run: str = "", max_fetch_age_hours: float = 48.0) -> bool:
    """Return True (force full re-fetch) if the external cache is missing or stale.

    Reads ``fetch-info.json`` from S3 (when ``S3_BUCKET`` is set) or from the
    local ``data/external-<run>/`` directory.  If the file is absent or the
    ``fetched_at`` timestamp is older than ``max_fetch_age_hours``, returns
    ``True`` so the caller passes ``force=True`` to ``nlm_ckn_fetch``.
    Otherwise returns ``False`` and the caller uses ``retry_empty=True`` to
    preserve cached data while retrying any previous failures.

    Parameters
    ----------
    run:
        Run name (selects ``data/external-<run>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    max_fetch_age_hours:
        Maximum acceptable cache age in hours.  Caches older than this
        trigger a full re-fetch.
    """
    logger = get_run_logger()
    fetch_info = None

    if S3_BUCKET:
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            boto3.client("s3").download_file(
                S3_BUCKET, "external/fetch-info.json", str(tmp_path)
            )
            fetch_info = json.loads(tmp_path.read_text())
            tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.info(f"Could not read fetch-info.json from S3: {exc}")
    else:
        info_path = _external_dir(run) / "fetch-info.json"
        if info_path.exists():
            try:
                fetch_info = json.loads(info_path.read_text())
            except Exception as exc:
                logger.warning(f"Could not parse fetch-info.json: {exc}")

    if fetch_info is None:
        logger.info("No fetch-info.json found — forcing full re-fetch")
        return True

    fetched_at = datetime.fromisoformat(fetch_info["fetched_at"])
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600

    if age_hours > max_fetch_age_hours:
        logger.info(
            f"External cache is {age_hours:.1f}h old (threshold: {max_fetch_age_hours}h)"
            " — forcing full re-fetch"
        )
        return True

    logger.info(
        f"External cache is {age_hours:.1f}h old (threshold: {max_fetch_age_hours}h)"
        " — reusing cache, retrying any previous failures"
    )
    return False


# ── Flow ───────────────────────────────────────────────────────────────────


@flow(name="nlm-ckn-release", log_prints=True)
def nlm_ckn_release(
    cell_kn_tag: str,
    ncbi_email: str = "",
    ncbi_api_key: str = "",
    run_name: str = "",
    github_repo: str = "rogermyung/cell-kn",
    tar_source: str = "",
    hubmap_urls_file: str = "",
    skip_ontology: bool = False,
    max_fetch_age_hours: float = 48.0,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """End-to-end NLM-CKN release pipeline driven by a cell-kn GitHub tag.

    Downloads the release tarball, fetches all external APIs fresh, then runs
    the three-phase ETL pipeline to produce a dated, versioned production
    artifact.

    Parameters
    ----------
    cell_kn_tag:
        Git tag on the cell-kn repository identifying the release, e.g.
        ``"v0.0.1"``.  Used to locate the GitHub Release asset and, if
        ``run_name`` is omitted, to derive the run name.
    ncbi_email:
        NCBI E-Utilities email.  Falls back to ``$NCBI_EMAIL``.
    ncbi_api_key:
        NCBI E-Utilities API key.  Falls back to ``$NCBI_API_KEY``.
    run_name:
        ETL run name (scopes all output directories).  Defaults to
        ``cell_kn_tag`` with any leading ``v`` stripped (e.g. ``"0.0.1"``).
    github_repo:
        ``owner/repo`` path for the cell-kn GitHub repository.  Used to
        construct the Release asset URL when ``tar_source`` is not given.
        The source repository may change over time.
    tar_source:
        Override URL or local path for the release tarball (``.tar.gz``).
        When omitted, the URL is derived from ``github_repo`` and
        ``cell_kn_tag`` using the ``prod-data-<tag>.tar.gz`` naming
        convention.
    hubmap_urls_file:
        Path to a local ``hubmap_urls.txt`` file.  HuBMap URLs are no longer
        bundled in the release archive and must be provided here.  When
        omitted the flow falls back to ``data/hubmap_urls.txt`` in the
        repository root.
    skip_ontology:
        Skip Phase 1 (ontology build) and reuse the existing baseline dump
        for this run.  Useful when re-running a release after a failed
        Phase 2 without repeating the expensive ontology build.
    max_fetch_age_hours:
        Maximum acceptable age of the external API cache before triggering a
        full re-fetch.  If ``fetch-info.json`` is younger than this threshold
        the existing cache is reused (with ``retry_empty=True`` to recover any
        previous failures).  If the cache is older or absent a full re-fetch is
        forced.  Defaults to 48 hours.
    java_opts:
        JVM flags passed to every Java invocation (default: ``-Xmx4g``).
    """
    logger = get_run_logger()

    run_name = run_name or cell_kn_tag.lstrip("v")
    logger.info(f"Release: tag={cell_kn_tag}  run={run_name}")

    # Derive tarball URL from tag if not explicitly provided.
    if not tar_source:
        tar_name = f"prod-data-{cell_kn_tag}.tar.gz"
        tar_source = (
            f"https://github.com/{github_repo}/releases/download"
            f"/{cell_kn_tag}/{tar_name}"
        )

    # ── Step 1: Extract release tarball ──────────────────────────────────
    try:
        extract_release_tarball(tar_source, run_name, hubmap_urls_file=hubmap_urls_file)
        sync_release_dir_to_s3(run=run_name)
    except Exception:
        logger.error(
            "Step 1 (extract release tarball) failed.\n"
            "To retry from this step:\n"
            f"  poetry run src/flows/release.py --tag {cell_kn_tag}"
        )
        raise

    # ── Step 2: Fetch external APIs ───────────────────────────────────────
    force_fetch = resolve_fetch_force(
        run=run_name, max_fetch_age_hours=max_fetch_age_hours
    )
    try:
        nlm_ckn_fetch(
            ncbi_email=ncbi_email,
            ncbi_api_key=ncbi_api_key,
            force=force_fetch,
            retry_empty=not force_fetch,
            run=run_name,
        )
    except Exception:
        logger.error(
            "Step 2 (fetch external APIs) failed.\n"
            "Already-fetched files in data/external-%s/ are intact.\n"
            "To resume fetching without re-downloading completed sources:\n"
            "  poetry run python src/DataFetcher.py --run %s\n"
            "Then re-run the full release to continue from Step 3:\n"
            "  poetry run src/flows/release.py --tag %s",
            run_name,
            run_name,
            cell_kn_tag,
        )
        raise

    # ── Step 3: Three-phase ETL ───────────────────────────────────────────
    try:
        nlm_ckn_etl(
            run_ontology=not skip_ontology,
            force_ontology=not skip_ontology,
            run_results=True,
            force_results=True,
            run_archive=True,
            force_archive=True,
            java_opts=java_opts,
            run=run_name,
        )
    except Exception:
        logger.error(
            "Step 3 (ETL pipeline) failed.\n"
            "External data in data/external-%s/ is complete.\n"
            "To retry the ETL without re-fetching:\n"
            "  poetry run python src/DataFetcher.py --run %s  # (will skip completed sources)\n"
            "  Then re-run the full release:\n"
            "  poetry run src/flows/release.py --tag %s",
            run_name,
            run_name,
            cell_kn_tag,
        )
        raise

    # Promote this release's results to the stable latest/ pointer so the
    # scheduled fetch targets the new gene set going forward.
    promote_results_to_latest(run=run_name)
    logger.info(f"Release {cell_kn_tag} complete (run={run_name})")


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NLM-CKN end-to-end release pipeline (Prefect)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--tag",
        required=True,
        dest="cell_kn_tag",
        help="cell-kn git tag, e.g. v2026-04",
    )
    parser.add_argument(
        "--ncbi-email",
        default=os.getenv("NCBI_EMAIL", ""),
        help="NCBI E-Utilities email (default: $NCBI_EMAIL)",
    )
    parser.add_argument(
        "--ncbi-api-key",
        default=os.getenv("NCBI_API_KEY", ""),
        help="NCBI E-Utilities API key (default: $NCBI_API_KEY)",
    )
    parser.add_argument(
        "--run-name",
        default="",
        help="ETL run name (default: tag with leading 'v' stripped)",
    )
    parser.add_argument(
        "--github-repo",
        default="rogermyung/cell-kn",
        help="GitHub owner/repo for cell-kn (default: rogermyung/cell-kn)",
    )
    parser.add_argument(
        "--tar-source",
        default="",
        help="Override tarball URL or local path (default: derived from --tag)",
    )
    parser.add_argument(
        "--hubmap-urls-file",
        default="",
        help="Path to local hubmap_urls.txt (default: data/hubmap_urls.txt in repo root)",
    )
    parser.add_argument(
        "--skip-ontology",
        action="store_true",
        help="Skip Phase 1 and reuse existing baseline dump",
    )
    parser.add_argument(
        "--max-fetch-age-hours",
        type=float,
        default=48.0,
        help=(
            "Maximum acceptable external cache age in hours before forcing a full re-fetch "
            "(default: 48). If the cache is younger, existing data is reused and only "
            "previously-failed entries are retried."
        ),
    )
    parser.add_argument(
        "--java-opts",
        default=DEFAULT_JAVA_OPTS,
        help=f"JVM flags (default: '{DEFAULT_JAVA_OPTS}')",
    )
    args = parser.parse_args()

    nlm_ckn_release(
        cell_kn_tag=args.cell_kn_tag,
        ncbi_email=args.ncbi_email,
        ncbi_api_key=args.ncbi_api_key,
        run_name=args.run_name,
        github_repo=args.github_repo,
        tar_source=args.tar_source,
        hubmap_urls_file=args.hubmap_urls_file,
        skip_ontology=args.skip_ontology,
        max_fetch_age_hours=args.max_fetch_age_hours,
        java_opts=args.java_opts,
    )
