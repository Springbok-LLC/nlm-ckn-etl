#!/usr/bin/env python3
"""Prefect fetch flow for the NLM-CKN ETL pipeline.

Downloads raw data from external APIs (CELLxGENE, Open Targets, NCBI Gene,
UniProt, HuBMAP) via ``DataFetcher.py`` and writes the results to local
storage and/or S3.

Designed to run independently on a schedule (via EventBridge + ECS
Fargate) without requiring ArangoDB or the full ETL pipeline.

Usage
-----
Run directly (no Prefect server needed)::

    cd python
    python src/flows/fetch.py
    python src/flows/fetch.py --ncbi-email user@example.com --ncbi-api-key KEY
    python src/flows/fetch.py --run-name sample

Or with the Prefect CLI after ``prefect server start``::

    prefect deployment run 'nlm-ckn-fetch/local'

See the README for full local-run and AWS deployment instructions.

Local vs S3 mode
----------------
When ``S3_BUCKET`` is unset (the default), all external cache files are
written to ``data/external/`` on the local filesystem only.  Set
``S3_BUCKET`` to push the cache to S3 after each successful fetch, making
it available to ``pipeline.py`` running on a different host.
"""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone

from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact

from _common import (
    REPO_ROOT,
    S3_BUCKET,
    _external_dir,
    _fetch_code_hash,
    _run_python_script,
    clean_empty_external_files,
    promote_external_staging,
    should_force_fetch,
    sync_external_from_s3,
    sync_external_to_s3_staging,
    validate_external_files,
)
from pipeline import sync_results_from_s3, validate_release_dir

# ── Tasks ──────────────────────────────────────────────────────────────────


@task(name="retry-failed-cache-entries", log_prints=True)
def retry_failed_cache_entries(run_name: str = "") -> None:
    """Remove empty ``{}`` entries from every JSON cache file in ``data/external-<name>/``.

    ``DataFetcher.py`` records a failed API call as an empty dict ``{}`` so
    the fetch loop skips it on the next run.  This task strips those entries
    *before* the fetcher runs, causing them to be retried while leaving all
    successfully-fetched data intact.

    Use this as the middle ground between no flags (resume, skip failures) and
    ``--force`` (wipe everything and start fresh).

    The special ``"gene_entrez_ids"`` bookkeeping key inside ``gene.json`` is
    preserved so the batch-checkpoint logic continues to work correctly.

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    external_dir = _external_dir(run_name)
    if not external_dir.is_dir():
        logger.info(f"{external_dir.name}/ does not exist — nothing to clean")
        return

    total_removed = 0
    for json_file in sorted(external_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Skipping {json_file.name}: {exc}")
            continue

        if not isinstance(data, dict):
            continue

        # Identify empty entries, preserving bookkeeping keys (e.g. "gene_entrez_ids")
        empty_keys = [
            k
            for k, v in data.items()
            if isinstance(v, dict) and not v and not k.endswith("_ids")
        ]
        if not empty_keys:
            logger.info(f"{json_file.name}: no empty entries")
            continue

        for k in empty_keys:
            del data[k]
        json_file.write_text(json.dumps(data, indent=4))
        logger.info(
            f"{json_file.name}: removed {len(empty_keys)} empty entr{'y' if len(empty_keys) == 1 else 'ies'}"
        )
        total_removed += len(empty_keys)

    logger.info(f"Total empty entries removed: {total_removed}")


@task(name="fetch-external-api-results", log_prints=True)
def fetch_external_api_results(
    arango_db_password: str = "",
    ncbi_email: str = "",
    ncbi_api_key: str = "",
    force: bool = False,
    max_source_age_hours: float = 0.0,
    run_name: str = "",
) -> None:
    """Run ``DataFetcher.py`` using the host Python interpreter.

    ArangoDB is not required for the fetch; ``ARANGO_DB_PASSWORD`` is
    forwarded for forward-compatibility but is ignored by the script.
    NCBI credentials are passed as environment variables.

    Parameters
    ----------
    force:
        Pass ``--force-all`` to ``DataFetcher.py``, bypassing all on-disk
        caches and re-fetching everything from scratch.  Use this for
        scheduled runs so stale or empty cache entries don't persist.
    max_source_age_hours:
        Hours of freshness before a source is re-fetched.  Sources whose
        last successful fetch is younger than this are skipped.  0 (default)
        disables the check and always re-fetches every source.
    run_name:
        Run name passed as ``--run-name`` to ``DataFetcher.py`` (selects
        ``data/results-<name>/``).  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    if force:
        logger.info("Force mode: ignoring on-disk cache, re-fetching all sources")
    if max_source_age_hours > 0:
        logger.info(
            f"Source max age: {max_source_age_hours}h — fresh sources will be skipped"
        )
    logger.info("Fetching external API results (DataFetcher)")
    extra_args = []
    if force:
        extra_args.append("--force-all")
    if max_source_age_hours > 0:
        extra_args.extend(["--max-source-age-hours", str(max_source_age_hours)])
    if run_name:
        extra_args.extend(["--run-name", run_name])
    _run_python_script(
        "DataFetcher.py",
        arango_db_password=arango_db_password,
        extra_env={
            "NCBI_EMAIL": ncbi_email,
            "NCBI_API_KEY": ncbi_api_key,
        },
        extra_args=extra_args or None,
    )
    logger.info("External API results fetched")


@task(name="transform-external-api-results", log_prints=True)
def transform_external_api_results(
    arango_db_password: str = "",
    force: bool = False,
    run_name: str = "",
) -> None:
    """Run ``DataTransformer.py`` to convert raw fetcher JSON into
    ``*_transformed.json`` files consumed by the tuple writers.

    Must run after ``fetch_external_api_results``.  The transformer is
    idempotent: it skips a source when the transformed output is newer than
    the raw input (unless ``force=True``).

    Parameters
    ----------
    force:
        Pass ``--force`` to re-run all transformers even when outputs are
        up to date.
    run_name:
        Run name passed as ``--run-name`` (selects ``data/results-<name>/``).
        Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()
    logger.info("Transforming external API results (DataTransformer)")
    extra_args = []
    if force:
        extra_args.append("--force")
    if run_name:
        extra_args.extend(["--run-name", run_name])
    _run_python_script(
        "DataTransformer.py",
        arango_db_password=arango_db_password,
        extra_args=extra_args or None,
    )
    logger.info("External API results transformed")


@task(name="record-fetch-artifact", log_prints=True)
def record_fetch_artifact(run_name: str = "", validated: bool = True) -> None:
    """Write ``fetch-info.json`` and a Prefect UI artifact summarising the run.

    ``fetch-info.json`` is stored alongside the cache files in
    ``data/external-<name>/`` so it travels to S3 with the
    ``sync_external_to_s3`` task.  ``pipeline.py`` reads it during the archive
    stage and merges its contents into ``build-info.txt``.

    Fields written:

    - ``fetched_at``       — ISO-8601 UTC timestamp
    - ``commit``           — short git commit hash of the repo at fetch time
    - ``fetch_code_hash``  — content hash of the fetch code (see _fetch_code_hash)
    - ``validated``        — whether validate_external_files passed
    - ``files``            — mapping of cache filename → byte size (``null`` if missing)

    Parameters
    ----------
    run_name:
        Run name (selects ``data/external-<name>/``).  Defaults to
        ``$CKN_RUN`` or ``'full'``.
    validated:
        ``False`` when recorded after a validation failure, so the data is kept
        (the next run resumes + re-validates) without being marked complete.
    """
    logger = get_run_logger()
    external_dir = _external_dir(run_name)

    # Collect file sizes for the required raw + transformed cache files
    required = [
        "cellxgene.json",
        "opentargets.json",
        "gene.json",
        "uniprot.json",
        "cellxgene_transformed.json",
        "opentargets_transformed.json",
        "gene_transformed.json",
        "uniprot_transformed.json",
    ]
    files_info: dict[str, int | None] = {}
    for name in required:
        path = external_dir / name
        files_info[name] = path.stat().st_size if path.exists() else None

    # Current git commit hash (best-effort; falls back to "unknown")
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()
    except Exception:
        commit = "unknown"

    info = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "fetch_code_hash": _fetch_code_hash(),
        "validated": validated,
        "files": files_info,
    }

    info_path = external_dir / "fetch-info.json"
    info_path.write_text(json.dumps(info, indent=2))
    logger.info(f"Fetch artifact written to {info_path.relative_to(REPO_ROOT)}")

    # Per-source status written by DataFetcher.py
    status_path = external_dir / "fetch-status.json"
    source_status: dict = {}
    if status_path.exists():
        try:
            source_status = json.loads(status_path.read_text())
        except Exception:
            pass

    # Prefect UI artifact — summary table visible in the flow-run page
    outcome_icon = {"ok": "✓", "failed": "✗", "skipped": "⏭"}
    file_rows = "\n".join(
        f"| `{name}` | {size:,} bytes |"
        if size is not None
        else f"| `{name}` | ⚠ missing |"
        for name, size in files_info.items()
    )
    source_names = ["cellxgene", "opentargets", "gene", "uniprot", "hubmap"]
    source_rows = "\n".join(
        "| `{name}` | {icon} {outcome} | {last_success} |".format(
            name=name,
            icon=outcome_icon.get(
                source_status.get(name, {}).get("last_outcome", ""), ""
            ),
            outcome=source_status.get(name, {}).get("last_outcome", "unknown"),
            last_success=source_status.get(name, {}).get("last_success_at", "—"),
        )
        for name in source_names
    )
    s3_note = (
        f"**S3 destination (shared cache):** `s3://{S3_BUCKET}/external/`"
        if S3_BUCKET
        else "_S3_BUCKET not set — files stored locally only._"
    )
    create_markdown_artifact(
        key="fetch-summary",
        markdown=f"""## External API Fetch Summary

**Fetched at:** {info["fetched_at"]}
**Commit:** `{commit}`
{s3_note}

### Source outcomes
| Source | Outcome | Last success |
|--------|---------|--------------|
{source_rows}

### Cache files
| File | Size |
|------|------|
{file_rows}
""",
        description="External API fetch results",
    )


# ── Flow ───────────────────────────────────────────────────────────────────


@flow(name="nlm-ckn-fetch", log_prints=True)
def nlm_ckn_fetch(
    ncbi_email: str = "",
    ncbi_api_key: str = "",
    force: bool = False,
    retry_empty: bool = False,
    max_source_age_hours: float = 0.0,
    max_fetch_age_hours: float = 0.0,
    run_name: str = "",
) -> None:
    """NLM-CKN external API fetch flow.

    Downloads raw data from CELLxGENE, Open Targets, NCBI Gene, UniProt,
    and HuBMAP into ``data/external/`` (local) and
    ``s3://${S3_BUCKET}/external/`` (when ``S3_BUCKET`` is set).

    Requires the release results directory (``data/results-<name>/``) to be
    populated first — either by running ``release.py`` or by syncing from S3.
    The NSForest results determine which genes are fetched from external APIs.

    Parameters
    ----------
    ncbi_email:
        NCBI E-Utilities email address.  Falls back to the ``NCBI_EMAIL``
        environment variable.
    ncbi_api_key:
        NCBI E-Utilities API key.  Falls back to the ``NCBI_API_KEY``
        environment variable.
    force:
        Re-fetch all data sources from scratch, ignoring any on-disk cache.
        Defaults to ``False`` (resume-friendly for development).  Scheduled
        runs should set this to ``True`` so stale or empty cache entries from
        previous runs are not carried forward.
    retry_empty:
        Strip empty ``{}`` cache entries before fetching, so previously-failed
        API calls are retried while all successfully-fetched data is kept.
        Ignored when ``force=True`` (force already discards everything).
    max_source_age_hours:
        Hours of freshness before a source is re-fetched.  Sources whose
        ``last_success_at`` in ``fetch-status.json`` is younger than this are
        skipped entirely, saving API quota and time.  0 (default) disables
        the check so every source is always re-fetched.  Ignored when
        ``force=True``.
    max_fetch_age_hours:
        Whole-cache max age in hours.  When > 0 and ``force`` is not already
        set, ``force`` is auto-resolved from the cached ``fetch-info.json``:
        a full re-fetch is forced if the cache is missing, older than this, or
        was produced by different fetch code (the ``fetch_code_hash`` changed);
        otherwise the cache is reused with ``retry_empty=True``.  0 (default)
        disables the check.  Used by the scheduled fetch in place of ``--force``.
    run_name:
        Run name passed to ``DataFetcher.py`` (selects
        ``data/results-<name>/``).  Defaults to ``$CKN_RUN`` or ``'full'``.
    """
    logger = get_run_logger()

    # Validate here too (not only in the CLI): Prefect-deployment runs call the
    # flow directly and would otherwise bypass the argparse check.
    if max_source_age_hours < 0:
        raise ValueError("max_source_age_hours must be non-negative")
    if max_fetch_age_hours < 0:
        raise ValueError("max_fetch_age_hours must be non-negative")

    # Resolve credentials: explicit parameters take priority, then env vars
    ncbi_email = ncbi_email or os.getenv("NCBI_EMAIL", "")
    ncbi_api_key = ncbi_api_key or os.getenv("NCBI_API_KEY", "")

    missing = [
        name
        for name, val in [("NCBI_EMAIL", ncbi_email), ("NCBI_API_KEY", ncbi_api_key)]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Required NCBI credential(s) not set: {', '.join(missing)}.\n"
            "Provide them via --ncbi-email / --ncbi-api-key flags, or set the "
            "NCBI_EMAIL / NCBI_API_KEY environment variables."
        )

    # ArangoDB password is not used by the fetcher but is forwarded to the
    # container env for forward-compatibility; ignore if unset.
    arango_db_password = os.getenv("ARANGO_DB_PASSWORD", "")

    # Resolve the run name once and thread the resolved value to every task.
    run_name = run_name or os.getenv("CKN_RUN", "full")

    if S3_BUCKET:
        logger.info(f"S3 mode: bucket={S3_BUCKET}")
    else:
        logger.info("Local mode: S3_BUCKET not set, writing to data/external/ only")

    # Auto-resolve force from cache age + fetch-code hash when a whole-cache max
    # age is given (the scheduled fetch) and force was not requested explicitly.
    if max_fetch_age_hours > 0 and not force:
        force = should_force_fetch(run_name, max_fetch_age_hours, logger.info)
        retry_empty = not force

    sync_results_from_s3(run_name=run_name)  # ensure release results are available
    # Fail fast if the release results are missing: the NSForest results drive
    # which genes the cellxgene/gene/uniprot/opentargets fetchers retrieve, so a
    # missing results dir means a silently incomplete fetch.
    validate_release_dir(run_name=run_name)
    sync_external_from_s3(run_name=run_name)  # restore external cache (no-op if no S3)
    clean_empty_external_files(run_name=run_name)
    if retry_empty and not force:
        retry_failed_cache_entries(run_name=run_name)
    fetch_external_api_results(
        arango_db_password=arango_db_password,
        ncbi_email=ncbi_email,
        ncbi_api_key=ncbi_api_key,
        force=force,
        max_source_age_hours=max_source_age_hours,
        run_name=run_name,
    )
    transform_external_api_results(
        arango_db_password=arango_db_password,
        force=force,
        run_name=run_name,
    )
    # Record the artifact even when validation fails, so the fetched data is not
    # discarded: the next run resumes + re-validates (see should_force_fetch)
    # instead of re-fetching from scratch.  A failed validation still re-raises
    # (and skips promotion below), so invalid data is never pushed to S3.
    try:
        validate_external_files(run_name=run_name)
    except Exception:
        record_fetch_artifact(run_name=run_name, validated=False)
        raise
    record_fetch_artifact(run_name=run_name, validated=True)
    sync_external_to_s3_staging(run_name=run_name)  # write to staging; pipeline.py is unaffected
    promote_external_staging(run_name=run_name)  # server-side copy runs/{run}/external-staging/ → external/


# ── CLI entry point ────────────────────────────────────────────────────────

def _parse_args(argv=None):
    """Build the fetch CLI parser, parse ``argv``, and validate.

    Extracted from ``__main__`` so tests can exercise the parser (flag names,
    negative-age rejection) in-process without spawning a subprocess.
    """
    parser = argparse.ArgumentParser(
        description="NLM-CKN external API fetch (Prefect)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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
        "--force",
        action="store_true",
        help=(
            "Re-fetch all data sources from scratch, ignoring any on-disk cache. "
            "Recommended for scheduled / production runs. "
            "Without this flag the fetcher resumes from cached data (useful during development)."
        ),
    )
    parser.add_argument(
        "--retry-empty",
        action="store_true",
        help=(
            "Retry only previously-failed API calls (those stored as empty {} in the cache) "
            "while keeping all successfully-fetched data. "
            "Useful during development to recover from transient errors without a full re-fetch. "
            "Ignored when --force is also set."
        ),
    )
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=0.0,
        help=(
            "Skip sources whose last successful fetch is younger than this many hours. "
            "0 (default) disables the check and always re-fetches every source. "
            "Ignored when --force is also set."
        ),
    )
    parser.add_argument(
        "--max-fetch-age-hours",
        type=float,
        default=0.0,
        help=(
            "Whole-cache max age in hours.  When > 0 (and --force is not set), "
            "auto-force a full re-fetch if the cache is missing, older than this, "
            "or produced by different fetch code; otherwise reuse + retry empties. "
            "0 (default) disables.  Used by the scheduled fetch in place of --force."
        ),
    )
    parser.add_argument(
        "--run-name",
        default=os.getenv("CKN_RUN", ""),
        help=(
            "Run name passed to DataFetcher.py (selects data/results-<name>/). "
            "Defaults to $CKN_RUN or 'full'."
        ),
    )
    args = parser.parse_args(argv)

    if args.max_source_age_hours < 0:
        parser.error("--max-source-age-hours must be non-negative")
    if args.max_fetch_age_hours < 0:
        parser.error("--max-fetch-age-hours must be non-negative")
    if not (args.ncbi_email and args.ncbi_api_key):
        parser.error(
            "Both NCBI email and API key are required. "
            "Use --ncbi-email and --ncbi-api-key, or set NCBI_EMAIL and NCBI_API_KEY "
            "environment variables."
        )
    return args


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    args = _parse_args()
    nlm_ckn_fetch(
        ncbi_email=args.ncbi_email,
        ncbi_api_key=args.ncbi_api_key,
        force=args.force,
        retry_empty=args.retry_empty,
        max_source_age_hours=args.max_source_age_hours,
        max_fetch_age_hours=args.max_fetch_age_hours,
        run_name=args.run_name,
    )
