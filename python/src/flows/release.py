#!/usr/bin/env python3
"""Prefect release flow for the NLM-CKN ETL pipeline.

Drives a full end-to-end release from an nlm-ckn GitHub Release tag:

1. Download the release tarball from GitHub Releases and extract it to
   ``data/results-<name>/`` (preserving the nested organ/dataset structure).
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
directly into ``data/results-<name>/``, consistent with what
``LoaderUtilities.get_dataset_file_paths`` expects.

HuBMap URLs are read from ``release.json`` at the repo root and written
into the results directory as ``hubmap_urls.txt`` so downstream code can
find them.

Usage
-----
Run directly::

    python src/flows/release.py --nlm-ckn-tag v2026-04 --ncbi-email user@example.com --ncbi-api-key KEY

Or via the Prefect CLI::

    prefect deployment run 'nlm-ckn-release/production' \\
        --param nlm_ckn_tag=v2026-04 \\
        --param ncbi_email=user@example.com \\
        --param ncbi_api_key=KEY

GitHub token
------------
Set ``GITHUB_TOKEN`` to authenticate against the GitHub API and avoid rate
limits.  Required for private repositories.
"""

import argparse
import csv
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3

from prefect import flow, get_run_logger, task

from _common import (
    DEFAULT_JAVA_OPTS,
    REPO_ROOT,
    S3_BUCKET,
    _s3_copy_prefix,
    _s3_sync,
    post_github_deployment_status,
    should_force_fetch,
)
from fetch import nlm_ckn_fetch
from pipeline import nlm_ckn_etl

# ── Tasks ──────────────────────────────────────────────────────────────────


_TARBALL_PREFIX = "data/prod/"


@task(name="extract-release-tarball", log_prints=True)
def extract_release_tarball(
    tar_source: str,
    run_name: str,
    hubmap_urls: list,
) -> Path:
    """Download (or copy) the nlm-ckn release tarball and extract it.

    The tarball (``prod-data-<tag>.tar.gz``) stores files under a nested
    ``data/prod/<organ>/...`` structure.  On extraction all files are flattened
    directly into ``data/results-<run_name>/``, discarding directory structure.
    Filenames are unique across datasets so no collisions occur.

    HuBMap URLs (from ``release.json``) are written to ``hubmap_urls.txt``
    inside the results directory so downstream code can find them.

    Parameters
    ----------
    tar_source:
        HTTPS URL, S3 URL, or local filesystem path to the ``.tar.gz`` file.
    run_name:
        Run name used to name the extraction directory
        (``data/results-<run_name>/``).
    hubmap_urls:
        List of HuBMap ASCT+B endpoint URLs from ``release.json``.

    Returns
    -------
    Path
        The extracted results directory (``data/results-<run_name>/``).
    """
    logger = get_run_logger()
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"

    # Resolve/download the tarball BEFORE touching results_dir, so a bad
    # --nlm-ckn-tag / --github-repo / --tar-source (404 or missing file) fails without
    # wiping an existing results directory.
    if tar_source.startswith("http://") or tar_source.startswith("https://"):
        tar_path = REPO_ROOT / "data" / f"release-{run_name}.tar.gz"
        logger.info(f"Downloading release tarball: {tar_source}")
        headers = {}
        token = os.getenv("GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(tar_source, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp, open(tar_path, "wb") as out:
                shutil.copyfileobj(resp, out)
        except urllib.error.HTTPError as exc:
            raise FileNotFoundError(
                f"Release tarball not found at {tar_source} (HTTP {exc.code}). "
                "Check --nlm-ckn-tag, --github-repo, or --tar-source."
            ) from exc
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
        if not tar_path.is_file():
            raise FileNotFoundError(
                f"Local release tarball not found: {tar_path}. Check --tar-source."
            )
        logger.info(f"Using local tarball: {tar_path}")

    # Source confirmed reachable — only now (re)create the results directory.
    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir(parents=True)

    # Extract, flattening all files into results_dir regardless of their nested
    # path within the tarball. Per-dataset result files carry organ/author/year
    # in their names, so they don't collide. The per-organ master_s3_manifest.csv
    # files, however, all share one basename and WOULD collide (last-writer-wins),
    # silently reducing the downstream integrity check to a single organ; they are
    # intercepted below and unioned into one complete manifest instead. This keeps
    # results_dir flat, consistent with what LoaderUtilities expects.
    logger.info(f"Extracting → {results_dir.name}/ (flat)")
    base_dir = results_dir.resolve()
    manifest_rows = {}  # filename -> s3_path, unioned across all per-organ manifests
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.name.startswith(_TARBALL_PREFIX):
                continue
            if member.isdir() or member.issym() or member.islnk():
                continue
            filename = Path(member.name).name
            if not filename:
                continue
            # Union per-organ manifests rather than flattening them onto one
            # basename (which would keep only the last-extracted organ's rows).
            if filename == "master_s3_manifest.csv":
                fobj = tf.extractfile(member)
                if fobj is not None:
                    reader = csv.DictReader(io.TextIOWrapper(fobj, encoding="utf-8"))
                    for row in reader:
                        fn = row.get("filename")
                        if fn:
                            manifest_rows[fn] = row.get("s3_path", "")
                continue
            resolved = (results_dir / filename).resolve()
            if not str(resolved).startswith(str(base_dir) + os.sep):
                continue
            member.name = filename
            tf.extract(member, results_dir)

    # Write the unioned manifest (complete across all organs) so the
    # LoaderUtilities cross-check validates the whole run, not just one organ.
    if manifest_rows:
        manifest_dst = results_dir / "master_s3_manifest.csv"
        with open(manifest_dst, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["filename", "s3_path"])
            for fn in sorted(manifest_rows):
                writer.writerow([fn, manifest_rows[fn]])
        logger.info(
            f"Unioned {len(manifest_rows)} manifest rows across all datasets"
        )

    # Remove downloaded tarball — contents are now in results_dir.
    if tar_path.parent == REPO_ROOT / "data" and tar_path.name.startswith("release-"):
        tar_path.unlink(missing_ok=True)

    # Write hubmap_urls.txt into results_dir from the in-memory list sourced
    # from release.json — no separate file transfer needed.
    hubmap_dst = results_dir / "hubmap_urls.txt"
    hubmap_dst.write_text("\n".join(hubmap_urls) + "\n")
    logger.info(f"HuBMap URLs written to {hubmap_dst.name} ({len(hubmap_urls)} entries)")

    # Validate that the extracted tarball actually contains NSForest result
    # files under the canonical results_ensg_*.csv naming.  An empty match means
    # the tarball uses an outdated convention (pre results_ensg/symbols split) or
    # is otherwise malformed — fail here, before the expensive fetch + ETL steps,
    # rather than silently building an empty graph downstream.
    nsforest_paths = list(results_dir.glob("results_ensg_*.csv"))
    if not nsforest_paths:
        raise FileNotFoundError(
            f"No results_ensg_*.csv files extracted to {results_dir.name}/ — the "
            "release tarball may use an outdated NSForest naming convention "
            "(pre results_ensg/symbols split) or be malformed."
        )
    logger.info(
        f"Extracted {len(nsforest_paths)} NSForest result files to {results_dir.name}/"
    )
    return results_dir


@task(name="sync-release-dir-to-s3", log_prints=True)
def sync_release_dir_to_s3(run_name: str = "") -> None:
    """Push ``data/results-<name>/`` to S3 so the pipeline can be re-run
    without re-downloading the zip.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name.  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping (local mode)")
        return
    run_name = run_name or os.getenv("CKN_RUN", "full")
    results_dir = REPO_ROOT / "data" / f"results-{run_name}"
    s3_dst = f"s3://{S3_BUCKET}/runs/{run_name}/01-results/"
    logger.info(f"Syncing {results_dir.name}/ → {s3_dst}")
    _s3_sync(str(results_dir), s3_dst)
    logger.info("Release dir pushed to S3")


@task(name="promote-results-to-latest", log_prints=True)
def promote_results_to_latest(run_name: str = "") -> None:
    """Server-side copy ``runs/<run>/01-results/`` → ``runs/latest/01-results/`` in S3.

    Keeps a stable pointer that the scheduled fetch Fargate task always reads
    from, so it uses the most recent release's gene set without requiring a
    CloudFormation redeploy when the run name changes.

    No-op when ``S3_BUCKET`` is empty (local-only mode).

    Parameters
    ----------
    run_name:
        Run name.  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if not S3_BUCKET:
        logger.info("S3_BUCKET not set — skipping (local mode)")
        return
    run_name = run_name or os.getenv("CKN_RUN", "full")
    src_prefix = f"runs/{run_name}/01-results/"
    dst_prefix = "runs/latest/01-results/"
    logger.info(
        f"Promoting s3://{S3_BUCKET}/{src_prefix} → s3://{S3_BUCKET}/{dst_prefix}"
    )
    count = _s3_copy_prefix(S3_BUCKET, src_prefix, dst_prefix)
    logger.info(f"Promoted {count} result file(s) to runs/latest/01-results/")


@task(name="resolve-fetch-force", log_prints=True)
def resolve_fetch_force(run_name: str = "", max_fetch_age_hours: float = 672.0) -> bool:
    """Return True (force full re-fetch) if the external cache is missing, stale,
    or was produced by different fetch code.

    Thin Prefect-task wrapper over :func:`_common.should_force_fetch` so the
    release flow and the scheduled fetch share one decision.  Forces when the
    cached ``fetch-info.json`` is absent, its ``fetch_code_hash`` no longer
    matches the current fetch code, or its ``fetched_at`` is older than
    ``max_fetch_age_hours``.  Otherwise returns ``False`` and the caller uses
    ``retry_empty=True`` to preserve cached data while retrying any failures.

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/`` in local mode).
    max_fetch_age_hours:
        Maximum acceptable cache age in hours.  Caches older than this trigger
        a full re-fetch.  Defaults to 672 (four weeks).
    """
    logger = get_run_logger()
    return should_force_fetch(run_name, max_fetch_age_hours, logger.info)


# ── Flow ───────────────────────────────────────────────────────────────────


@flow(name="nlm-ckn-release", log_prints=True)
def nlm_ckn_release(
    nlm_ckn_tag: str,
    ncbi_email: str = "",
    ncbi_api_key: str = "",
    run_name: str = "",
    github_repo: str = "NIH-NLM/nlm-ckn",
    tar_source: str = "",
    release_config: str = "",
    max_fetch_age_hours: float = 672.0,
    java_opts: str = DEFAULT_JAVA_OPTS,
) -> None:
    """End-to-end NLM-CKN release pipeline driven by an nlm-ckn GitHub tag.

    Downloads the release tarball, fetches all external APIs fresh, then runs
    the three-phase ETL pipeline to produce a dated, versioned production
    artifact.

    Parameters
    ----------
    nlm_ckn_tag:
        Git tag on the nlm-ckn repository identifying the release, e.g.
        ``"v0.0.1"``.  Used to locate the GitHub Release asset and, if
        ``run_name`` is omitted, to derive the run name.
    ncbi_email:
        NCBI E-Utilities email.  Falls back to ``$NCBI_EMAIL``.
    ncbi_api_key:
        NCBI E-Utilities API key.  Falls back to ``$NCBI_API_KEY``.
    run_name:
        ETL run name (scopes all output directories).  Defaults to
        ``nlm_ckn_tag`` with any leading ``v`` stripped (e.g. ``"0.0.1"``).
    github_repo:
        ``owner/repo`` path for the nlm-ckn GitHub repository.  Used to
        construct the Release asset URL when ``tar_source`` is not given.
        The source repository may change over time.
    tar_source:
        Override URL or local path for the release tarball (``.tar.gz``).
        When omitted, the URL is derived from ``github_repo`` and
        ``nlm_ckn_tag`` using the ``prod-data-<tag>.tar.gz`` naming
        convention.
    release_config:
        Path or S3 URL for ``release.json``, read for ``hubmap_urls``.  On the
        CLI this same file also seeds the defaults for ``--nlm-ckn-tag``,
        ``--github-repo``, ``--tar-source``, and ``--max-fetch-age-hours``.
        Defaults to ``release.json`` in the repository root.
    max_fetch_age_hours:
        Maximum acceptable age of the external API cache before triggering a
        full re-fetch.  If ``fetch-info.json`` is younger than this threshold
        the existing cache is reused (with ``retry_empty=True`` to recover any
        previous failures).  If the cache is older or absent a full re-fetch is
        forced.  Defaults to 672 hours (four weeks).
    java_opts:
        JVM flags passed to every Java invocation (default: ``DEFAULT_JAVA_OPTS``,
        currently ``-Xmx32g``).
    """
    logger = get_run_logger()

    # Validate here too (not only in the CLI): Prefect-deployment runs call the
    # flow directly and would otherwise bypass the argparse check.
    if max_fetch_age_hours < 0:
        raise ValueError("max_fetch_age_hours must be non-negative")

    run_name = run_name or nlm_ckn_tag.lstrip("v")
    logger.info(f"Release: tag={nlm_ckn_tag}  run={run_name}")

    start = datetime.now(timezone.utc)

    cfg = _read_release_json(release_config)
    hubmap_urls = cfg.get("hubmap_urls", [])
    if not hubmap_urls:
        raise ValueError("hubmap_urls is empty in release.json — cannot proceed")

    # Guard against the default placeholder values being committed unchanged.
    # Validate against the effective values after --nlm-ckn-tag/--tar-source overrides.
    _PLACEHOLDER_TAG = "v0.0.0-alpha"
    if nlm_ckn_tag == _PLACEHOLDER_TAG:
        raise ValueError(
            f"nlm_ckn_tag is still the default placeholder ({nlm_ckn_tag!r}). "
            "Provide a real release tag via --nlm-ckn-tag or update nlm_ckn_tag in release.json."
        )
    # Derive tarball URL from tag if not explicitly provided.
    if not tar_source:
        tar_name = f"prod-data-{nlm_ckn_tag}.tar.gz"
        tar_source = (
            f"https://github.com/{github_repo}/releases/download"
            f"/{nlm_ckn_tag}/{tar_name}"
        )
    if _PLACEHOLDER_TAG in tar_source:
        raise ValueError(
            f"tar_source still contains the default placeholder ({tar_source!r}). "
            "Update tar_source to a real release URL or provide --tar-source."
        )

    # ── Step 1: Extract release tarball ──────────────────────────────────
    post_github_deployment_status(
        state="in_progress",
        description=f"[1/3] Extracting release tarball for {nlm_ckn_tag}",
    )
    try:
        extract_release_tarball(tar_source, run_name, hubmap_urls)
        sync_release_dir_to_s3(run_name=run_name)
    except Exception as exc:
        logger.error(
            "Step 1 (extract release tarball) failed.\n"
            "To retry from this step:\n"
            f"  poetry run src/flows/release.py --nlm-ckn-tag {nlm_ckn_tag}"
        )
        post_github_deployment_status(
            state="failure",
            description=f"[1/3] Failed extracting tarball: {exc}"[:140],
        )
        raise

    # ── Step 2: Fetch external APIs ───────────────────────────────────────
    force_fetch = resolve_fetch_force(
        run_name=run_name, max_fetch_age_hours=max_fetch_age_hours
    )
    post_github_deployment_status(
        state="in_progress",
        description=f"[2/3] Fetching external APIs ({'force' if force_fetch else 'incremental'})",
    )
    try:
        nlm_ckn_fetch(
            ncbi_email=ncbi_email,
            ncbi_api_key=ncbi_api_key,
            force=force_fetch,
            retry_empty=not force_fetch,
            run_name=run_name,
        )
    except Exception as exc:
        logger.error(
            "Step 2 (fetch external APIs) failed.\n"
            "Already-fetched files in data/external-%s/ are intact.\n"
            "To resume fetching without re-downloading completed sources:\n"
            "  poetry run python src/DataFetcher.py --run-name %s\n"
            "Then re-run the full release to continue from Step 3:\n"
            "  poetry run src/flows/release.py --nlm-ckn-tag %s",
            run_name,
            run_name,
            nlm_ckn_tag,
        )
        post_github_deployment_status(
            state="failure",
            description=f"[2/3] Failed fetching external APIs: {exc}"[:140],
        )
        raise

    # ── Step 3: Three-phase ETL ───────────────────────────────────────────
    post_github_deployment_status(
        state="in_progress",
        description=f"[3/3] Running ETL pipeline for {nlm_ckn_tag}",
    )
    try:
        nlm_ckn_etl(
            run_ontology=True,
            force_ontology=True,
            run_results=True,
            force_results=True,
            run_archive=True,
            force_archive=True,
            java_opts=java_opts,
            run_name=run_name,
        )
    except Exception as exc:
        logger.error(
            "Step 3 (ETL pipeline) failed.\n"
            "External data in data/external-%s/ is complete.\n"
            "To retry the ETL without re-fetching:\n"
            "  poetry run python src/DataFetcher.py --run-name %s  # (will skip completed sources)\n"
            "  Then re-run the full release:\n"
            "  poetry run src/flows/release.py --nlm-ckn-tag %s",
            run_name,
            run_name,
            nlm_ckn_tag,
        )
        post_github_deployment_status(
            state="failure",
            description=f"[3/3] ETL pipeline failed: {exc}"[:140],
        )
        raise

    # Promote this release's results to the stable latest/ pointer so the
    # scheduled fetch targets the new gene set going forward.
    try:
        promote_results_to_latest(run_name=run_name)
    except Exception as exc:
        logger.error("Promotion of %s failed: %s", nlm_ckn_tag, exc)
        post_github_deployment_status(
            state="failure",
            description=f"Promotion of {nlm_ckn_tag} failed: {exc}"[:140],
        )
        raise

    logger.info(f"Release {nlm_ckn_tag} complete (run={run_name})")
    elapsed = datetime.now(timezone.utc) - start
    minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
    post_github_deployment_status(
        state="success",
        description=f"Released {nlm_ckn_tag} in {minutes}m {seconds}s",
    )


# ── CLI entry point ────────────────────────────────────────────────────────

def _read_release_json(release_config: str = "") -> dict:
    """Read release.json from a local path or S3 URL."""
    path = release_config or str(REPO_ROOT / "release.json")
    if path.startswith("s3://"):
        without_scheme = path[len("s3://"):]
        bucket, _, key = without_scheme.partition("/")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            boto3.client("s3").download_file(bucket, key, str(tmp_path))
            return json.loads(tmp_path.read_text())
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def _load_release_json(release_config: str = "") -> dict:
    """Seed os.environ defaults from a release.json (setdefault).

    Reads ``release_config`` (a local path or S3 URL) when given, otherwise the
    repo-root ``release.json``.  Only sets keys not already present, so real env
    vars and CLI flags win.  Returns the parsed dict.
    """
    data = _read_release_json(release_config)
    os.environ.setdefault("NLM_CKN_TAG", str(data.get("nlm_ckn_tag") or ""))
    os.environ.setdefault("GITHUB_REPO", str(data.get("github_repo") or "NIH-NLM/nlm-ckn"))
    os.environ.setdefault("TAR_SOURCE", str(data.get("tar_source") or ""))
    if data.get("max_fetch_age_hours") is not None:
        os.environ.setdefault("MAX_FETCH_AGE_HOURS", str(data["max_fetch_age_hours"]))
    return data


def _save_release_json(updates: dict) -> None:
    """Merge *updates* into release.json, preserving existing keys and order."""
    config_path = REPO_ROOT / "release.json"
    data = json.loads(config_path.read_text()) if config_path.exists() else {}
    data.update({k: v for k, v in updates.items() if v is not None})
    config_path.write_text(json.dumps(data, indent=4) + "\n")
    print(f"[release] Saved config to {config_path}")


def _parse_args(argv=None):
    """Build the release CLI parser, parse ``argv``, and validate.

    Extracted from ``__main__`` so tests can exercise the parser (flag names,
    negative-age rejection) in-process without spawning a subprocess.
    """
    parser = argparse.ArgumentParser(
        description="NLM-CKN end-to-end release pipeline (Prefect)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--nlm-ckn-tag",
        required=not os.getenv("NLM_CKN_TAG"),
        default=os.getenv("NLM_CKN_TAG", ""),
        help="Upstream nlm-ckn git tag, e.g. v2026-04 (default: nlm_ckn_tag from release.json)",
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
        default=os.getenv("GITHUB_REPO", "NIH-NLM/nlm-ckn"),
        help="GitHub owner/repo for nlm-ckn (default: github_repo from release.json)",
    )
    parser.add_argument(
        "--tar-source",
        default=os.getenv("TAR_SOURCE", ""),
        help="Override tarball URL or local path (default: tar_source from release.json, or derived from --nlm-ckn-tag)",
    )
    parser.add_argument(
        "--release-config",
        default="",
        help="Path or S3 URL for release.json (default: release.json in repo root)",
    )
    parser.add_argument(
        "--max-fetch-age-hours",
        type=float,
        default=float(os.getenv("MAX_FETCH_AGE_HOURS") or 672.0),
        help="Maximum external cache age in hours before forcing a re-fetch (default: max_fetch_age_hours from release.json, or 672 = four weeks)",
    )
    parser.add_argument(
        "--java-opts",
        default=DEFAULT_JAVA_OPTS,
        help=f"JVM flags (default: '{DEFAULT_JAVA_OPTS}')",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        help=(
            "Write effective --nlm-ckn-tag, --tar-source, --github-repo, "
            "and --max-fetch-age-hours values back to release.json before running."
        ),
    )
    args = parser.parse_args(argv)

    if args.max_fetch_age_hours < 0:
        parser.error("--max-fetch-age-hours must be non-negative")
    return args


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    # Resolve --release-config first so the file it points to (not just the
    # repo-root release.json) seeds every env-backed default below.
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--release-config", default="")
    _load_release_json(_pre.parse_known_args()[0].release_config)

    args = _parse_args()

    if args.save_config:
        _save_release_json({
            "nlm_ckn_tag": args.nlm_ckn_tag,
            "github_repo": args.github_repo,
            "tar_source": args.tar_source,
            "max_fetch_age_hours": args.max_fetch_age_hours,
        })

    nlm_ckn_release(
        nlm_ckn_tag=args.nlm_ckn_tag,
        ncbi_email=args.ncbi_email,
        ncbi_api_key=args.ncbi_api_key,
        run_name=args.run_name,
        github_repo=args.github_repo,
        tar_source=args.tar_source,
        release_config=args.release_config,
        max_fetch_age_hours=args.max_fetch_age_hours,
        java_opts=args.java_opts,
    )
