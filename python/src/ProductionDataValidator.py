#!/usr/bin/env python3
"""Validate the nlm-ckn ``data/prod`` tree against the ETL's assumptions.

The ETL imposes naming-convention and content assumptions on the upstream
``data/prod`` files (NSForest results, dataset summaries, silhouette scores,
author-to-CL mappings, CELLxGENE harvester output, S3 manifests).  Many of
those assumptions fail *silently* downstream — an inner merge that drops
rows, a dataset skipped for a malformed CL id, a companion file missed
because its suffix does not match.  This script checks them up front so a
release can be rejected before the expensive fetch + ETL stages run.

It validates the NESTED prod tree (the source of truth), simulating the
flatten that ``release.extract_release_tarball`` performs so post-flatten
collisions are caught without packaging a tarball.

The canonical patterns and column lists come from :mod:`ProductionDataSpecification`,
shared with ``LoaderUtilities`` so the validator cannot drift from the
readers.

Scope: structural checks (discovery, companion pairing, post-flatten
uniqueness, manifest membership); silent-failure content checks (required
columns, cluster_header⇔silhouette pairing, silhouette join-column name,
dataset_version_id presence, CL-CURIE conformance); value-format checks
(numeric clusterSize, parseable gene-list columns); and cross-file
referential integrity (mapping↔results clusters, harvester/mapping dvid
coverage, tissue CURIE tokens).

Usage
-----
::

    python src/ProductionDataValidator.py --data-dir /path/to/nlm-ckn/data/prod
    python src/ProductionDataValidator.py --data-dir DIR --json
    python src/ProductionDataValidator.py --data-dir DIR --strict   # warnings fail too

Exit status is non-zero when any ERROR (or, with ``--strict``, any WARN)
is found.
"""

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import ProductionDataSpecification as spec

ERROR = "ERROR"
WARN = "WARN"


@dataclass
class Finding:
    """A single validation result.

    Parameters
    ----------
    severity : str
        :data:`ERROR` or :data:`WARN`.
    check : str
        Short stable identifier for the check (e.g. ``"companion-missing"``).
    message : str
        Human-readable description of what failed.
    path : str
        Relative path (to the data dir) of the offending file, or ``""``.
    """

    severity: str
    check: str
    message: str
    path: str = ""


@dataclass
class Report:
    """Accumulated findings for a validation run."""

    data_dir: Path
    findings: list = field(default_factory=list)

    def add(self, severity, check, message, path=""):
        """Record a finding."""
        self.findings.append(Finding(severity, check, message, str(path)))

    def errors(self):
        """Return all ERROR-severity findings."""
        return [f for f in self.findings if f.severity == ERROR]

    def warnings(self):
        """Return all WARN-severity findings."""
        return [f for f in self.findings if f.severity == WARN]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_header(path):
    """Return the column list of a CSV without loading its rows.

    Returns ``None`` if the file cannot be parsed.
    """
    try:
        return list(pd.read_csv(path, nrows=0).columns)
    except Exception as exc:  # malformed/unreadable CSV
        return exc


def _rel(report, path):
    """Render *path* relative to the data dir for compact messages."""
    try:
        return str(Path(path).relative_to(report.data_dir))
    except ValueError:
        return str(path)


def _companions(data_dir, nsforest_name, prefix):
    """Return paths of the named companion for an NSForest results file."""
    return list(data_dir.glob(f"**/{spec.companion_basename(nsforest_name, prefix)}"))


def _is_string_list(value):
    """True if *value* parses as a Python list literal (mirrors parse_string_list)."""
    try:
        return isinstance(ast.literal_eval(str(value)), list)
    except (ValueError, SyntaxError):
        return False


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def check_structure(report):
    """Discovery, companion pairing, post-flatten uniqueness.

    Returns the list of discovered ``results_ensg_*.csv`` paths so content
    checks can reuse it.
    """
    data_dir = report.data_dir
    nsforest_paths = sorted(data_dir.glob(spec.NSFOREST_GLOB))

    if not nsforest_paths:
        report.add(
            ERROR,
            "no-results",
            f"No {spec.NSFOREST_PREFIX}_*.csv files found under {data_dir} — "
            "an empty graph would be built downstream.",
        )
        return nsforest_paths

    # Companion pairing: summary required, silhouette/mapping optional.  A
    # companion is located by prefix substitution, mirroring the reader.
    for p in nsforest_paths:
        for label, prefix in spec.COMPANION_PREFIXES.items():
            want = spec.companion_basename(p.name, prefix)
            matches = list(data_dir.glob(f"**/{want}"))
            if label == "summary" and not matches:
                report.add(
                    ERROR,
                    "companion-missing",
                    f"Required summary companion {want!r} not found for "
                    f"{p.name} (dataset version id lookup would fail).",
                    _rel(report, p),
                )
            if len(matches) > 1:
                report.add(
                    WARN,
                    "companion-ambiguous",
                    f"{len(matches)} files match {label} companion {want!r}; "
                    "the reader uses the first.",
                    _rel(report, p),
                )

    # Post-flatten basename uniqueness among CONSUMED file kinds (others
    # collide harmlessly).  master_s3_manifest.csv is unioned, so excluded.
    seen = {}
    for p in data_dir.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if name in spec.FLATTEN_COLLISION_ALLOWED:
            continue
        if not any(name.startswith(pre) for pre in spec.CONSUMED_PREFIXES) and not (
            name.endswith("_harvester_final.csv")
        ):
            continue
        seen.setdefault(name, []).append(p)
    for name, paths in seen.items():
        if len(paths) > 1:
            locs = ", ".join(_rel(report, p) for p in paths)
            report.add(
                ERROR,
                "flatten-collision",
                f"Basename {name!r} occurs {len(paths)}× ({locs}); flattening "
                "into one dir would silently drop all but the last.",
            )

    return nsforest_paths


def check_manifest(report):
    """Manifest header and results membership cross-check."""
    data_dir = report.data_dir
    manifest_paths = list(data_dir.rglob(spec.MANIFEST_NAME))
    if not manifest_paths:
        report.add(
            WARN,
            "manifest-absent",
            f"No {spec.MANIFEST_NAME} found; the extraction-time integrity "
            "cross-check would be skipped.",
        )
        return

    listed = set()
    for mp in manifest_paths:
        cols = _read_header(mp)
        if isinstance(cols, Exception):
            report.add(ERROR, "manifest-unreadable", f"{cols}", _rel(report, mp))
            continue
        missing = [c for c in spec.MANIFEST_REQUIRED_COLUMNS if c not in cols]
        if missing:
            report.add(
                ERROR,
                "manifest-columns",
                f"{spec.MANIFEST_NAME} missing column(s) {missing}.",
                _rel(report, mp),
            )
            continue
        df = pd.read_csv(mp)
        for fn in df["filename"].dropna().astype(str):
            if fn.startswith(f"{spec.NSFOREST_PREFIX}_") and fn.endswith(".csv"):
                listed.add(fn)

    found = {p.name for p in data_dir.glob(spec.NSFOREST_GLOB)}
    for fn in sorted(listed - found):
        report.add(
            ERROR,
            "manifest-missing-file",
            f"{fn} is listed in a manifest but not present in the tree.",
        )
    for fn in sorted(found - listed):
        report.add(
            WARN,
            "manifest-unlisted-file",
            f"{fn} is present but not listed in any manifest.",
        )


# ---------------------------------------------------------------------------
# Content / silent-failure checks
# ---------------------------------------------------------------------------


def _check_required_columns(report, path, required, check, kind):
    """Emit an ERROR for any missing required column; return the header.

    Returns ``None`` if the file is unreadable.
    """
    cols = _read_header(path)
    if isinstance(cols, Exception):
        report.add(ERROR, "unreadable", f"{kind} unreadable: {cols}", _rel(report, path))
        return None
    missing = [c for c in required if c not in cols]
    if missing:
        report.add(
            ERROR,
            check,
            f"{kind} missing required column(s) {missing}.",
            _rel(report, path),
        )
    return cols


def check_content(report, nsforest_paths):
    """Required columns and cross-file silent-failure checks per dataset."""
    data_dir = report.data_dir

    for p in nsforest_paths:
        ns_cols = _check_required_columns(
            report, p, spec.NSFOREST_REQUIRED_COLUMNS, "nsforest-columns", "results_ensg"
        )

        # Summary: required column + at least one non-null dataset_version_id.
        summary_name = spec.companion_basename(p.name, spec.SUMMARY_PREFIX)
        for sp in data_dir.glob(f"**/{summary_name}"):
            s_cols = _check_required_columns(
                report,
                sp,
                spec.SUMMARY_REQUIRED_COLUMNS,
                "summary-columns",
                "master_dataset_summary",
            )
            if s_cols and "dataset_version_id" in s_cols:
                sdf = pd.read_csv(sp)
                if sdf["dataset_version_id"].dropna().empty:
                    report.add(
                        ERROR,
                        "summary-no-dvid",
                        "dataset_version_id column has no non-null value "
                        "(get_dataset_version_id_lists would raise).",
                        _rel(report, sp),
                    )

        # Silhouette: stat columns + cross-file cluster_header dependency.
        sil_name = spec.companion_basename(p.name, spec.SILHOUETTE_PREFIX)
        sil_matches = list(data_dir.glob(f"**/{sil_name}"))
        if sil_matches:
            sil = sil_matches[0]
            sil_cols = _check_required_columns(
                report,
                sil,
                spec.SILHOUETTE_REQUIRED_COLUMNS,
                "silhouette-columns",
                "silhouette_fscore_summary",
            )
            # cluster_header⇔silhouette: when a silhouette file exists, the
            # results file MUST carry cluster_header (read at row 0).
            if ns_cols is not None and "cluster_header" not in ns_cols:
                report.add(
                    ERROR,
                    "cluster-header-missing",
                    f"{p.name} has a silhouette companion but no "
                    "'cluster_header' column (NSForest merge would crash).",
                    _rel(report, p),
                )
            elif ns_cols is not None and sil_cols is not None:
                # The silhouette join column NAME equals the cluster_header value.
                try:
                    ch_val = str(pd.read_csv(p, usecols=["cluster_header"]).iloc[0, 0])
                except Exception as exc:
                    ch_val = None
                    report.add(
                        WARN,
                        "cluster-header-unreadable",
                        f"Could not read cluster_header value: {exc}",
                        _rel(report, p),
                    )
                if ch_val is not None and ch_val not in sil_cols:
                    report.add(
                        ERROR,
                        "silhouette-join-column",
                        f"silhouette file lacks join column {ch_val!r} "
                        "(the results cluster_header value); merge yields no rows.",
                        _rel(report, sil),
                    )

        # Mapping: required columns + CL-CURIE conformance (rows the reader
        # would warn-and-skip).
        map_name = spec.companion_basename(p.name, spec.MAPPING_PREFIX)
        for mp in data_dir.glob(f"**/{map_name}"):
            m_cols = _check_required_columns(
                report,
                mp,
                spec.MAPPING_REQUIRED_COLUMNS,
                "mapping-columns",
                "cluster_cid_mapping",
            )
            if m_cols and {"manual_mapped_cid", "cell_ontology_id"} <= set(m_cols):
                mdf = pd.read_csv(mp)
                bad = 0
                for _, row in mdf.iterrows():
                    raw = row.get("manual_mapped_cid")
                    if pd.isna(raw) or str(raw).strip() == "":
                        raw = row.get("cell_ontology_id")
                    if pd.isna(raw) or str(raw).strip() == "":
                        bad += 1
                        continue
                    if not spec.CL_CURIE_RE.match(spec.obo_purl_to_curie(str(raw))):
                        bad += 1
                if bad:
                    report.add(
                        WARN,
                        "mapping-cl-curie",
                        f"{bad}/{len(mdf)} mapping rows have no valid CL CURIE "
                        "(manual_mapped_cid/cell_ontology_id); rows are skipped.",
                        _rel(report, mp),
                    )

    # Harvester: required join column on every harvester file.
    for hp in data_dir.rglob("*_harvester_final.csv"):
        _check_required_columns(
            report,
            hp,
            spec.HARVESTER_REQUIRED_COLUMNS,
            "harvester-columns",
            "harvester_final",
        )

    # UBERON: required columns, and a root term to roll cell sets up to.
    for up in data_dir.rglob(spec.UBERON_GLOB):
        u_cols = _check_required_columns(
            report,
            up,
            spec.UBERON_REQUIRED_COLUMNS,
            "uberon-columns",
            "uberon",
        )
        if u_cols and "level" in u_cols:
            udf = pd.read_csv(up)
            if not (udf["level"] == spec.UBERON_ROOT_LEVEL).any():
                report.add(
                    ERROR,
                    "uberon-root",
                    f"No row with level == {spec.UBERON_ROOT_LEVEL}, so cell sets "
                    "of this organ cannot be connected to a root UBERON term.",
                    _rel(report, up),
                )


# ---------------------------------------------------------------------------
# Value-format checks (P1)
# ---------------------------------------------------------------------------


def check_value_formats(report, nsforest_paths):
    """Per-row value-format checks on results files.

    These guard the *crash* paths: ``collect_unique_gene_names``
    ast.literal_evals the gene-list columns without a guard, and the
    cluster-size filter compares ``clusterSize`` numerically — a malformed
    value in either raises during the run, so both are ERROR.
    """
    for p in nsforest_paths:
        try:
            df = pd.read_csv(p)
        except Exception:
            continue  # unreadable already reported by check_content

        if "clusterSize" not in df.columns:
            continue  # missing-column already reported

        sizes = pd.to_numeric(df["clusterSize"], errors="coerce")
        nonnumeric = int((sizes.isna() & df["clusterSize"].notna()).sum())
        if nonnumeric:
            report.add(
                ERROR,
                "clustersize-nonnumeric",
                f"{nonnumeric} row(s) have a non-numeric clusterSize "
                "(the cluster-size filter would raise).",
                _rel(report, p),
            )

        # Only large clusters are parsed downstream, so only they can crash.
        large = (sizes >= spec.MIN_CLUSTER_SIZE).fillna(False)
        for col in spec.GENE_LIST_COLUMNS:
            if col not in df.columns:
                continue
            bad = sum(1 for v in df.loc[large, col] if not _is_string_list(v))
            if bad:
                report.add(
                    ERROR,
                    "gene-list-unparseable",
                    f"{bad} large-cluster row(s) have an unparseable {col} "
                    "(not a Python list literal); gene collection would raise.",
                    _rel(report, p),
                )


# ---------------------------------------------------------------------------
# Cross-file referential integrity (P1)
# ---------------------------------------------------------------------------


def _harvester_dvids(data_dir):
    """Union of dataset_version_id across all harvester files (reader
    concatenates them all)."""
    dvids = set()
    for hp in data_dir.rglob("*_harvester_final.csv"):
        try:
            h = pd.read_csv(hp, usecols=["dataset_version_id"])
        except Exception:
            continue
        dvids |= set(h["dataset_version_id"].dropna().astype(str))
    return dvids


def check_referential(report, nsforest_paths):
    """Cross-file checks: mapping↔results clusters, dvid coverage, tissue CURIEs.

    These are silent-degradation paths (dropped merge rows, lost metadata),
    so they are WARN rather than ERROR.
    """
    data_dir = report.data_dir
    harvester_dvids = _harvester_dvids(data_dir)
    uberon_organs = {
        spec.organ_of_uberon_path(up) for up in data_dir.rglob(spec.UBERON_GLOB)
    }

    for p in nsforest_paths:
        try:
            rdf = pd.read_csv(p)
        except Exception:
            continue
        results_clusters = (
            set(rdf["clusterName"].dropna().astype(str))
            if "clusterName" in rdf.columns
            else set()
        )

        # Summary: collect dvids and sanity-check tissue CURIE tokens.
        summary_dvids = set()
        for sp in _companions(data_dir, p.name, spec.SUMMARY_PREFIX):
            try:
                sdf = pd.read_csv(sp)
            except Exception:
                continue
            if "dataset_version_id" in sdf.columns:
                summary_dvids |= set(sdf["dataset_version_id"].dropna().astype(str))
            if "tissue_ontology_term_id" in sdf.columns:
                bad = set()
                for raw in sdf["tissue_ontology_term_id"].dropna().astype(str):
                    for tok in (t.strip() for t in raw.split("|")):
                        if tok and not spec.ONTOLOGY_CURIE_RE.match(tok):
                            bad.add(tok)
                if bad:
                    report.add(
                        WARN,
                        "tissue-curie",
                        f"tissue_ontology_term_id has non-CURIE token(s): "
                        f"{sorted(bad)}",
                        _rel(report, sp),
                    )
            if "organ" in sdf.columns:
                unrooted = {
                    spec.normalize_organ(o)
                    for o in sdf["organ"].dropna().astype(str)
                    if spec.normalize_organ(o) not in uberon_organs
                    and spec.normalize_organ(o) not in spec.ORGAN_ROOT_OVERRIDES
                }
                if unrooted:
                    report.add(
                        WARN,
                        "uberon-root-missing",
                        f"organ(s) {sorted(unrooted)} have neither a "
                        f"{spec.UBERON_GLOB} file nor an ORGAN_ROOT_OVERRIDES entry; "
                        "cell sets keep an edge to every tissue term.",
                        _rel(report, sp),
                    )

        # Harvester enrichment: every summary dvid should have a harvester row.
        missing_h = summary_dvids - harvester_dvids
        if missing_h:
            report.add(
                WARN,
                "harvester-dvid-missing",
                f"{len(missing_h)} summary dataset_version_id(s) have no harvester "
                f"row; CSD metadata enrichment is lost: {sorted(missing_h)}",
                _rel(report, p),
            )

        # Mapping: cluster names must be a subset of results clusters (the
        # inner merge silently drops the rest); mapping dvids ⊆ summary dvids.
        for mp in _companions(data_dir, p.name, spec.MAPPING_PREFIX):
            try:
                mdf = pd.read_csv(mp)
            except Exception:
                continue
            if "cluster_name" in mdf.columns and results_clusters:
                orphan = set(mdf["cluster_name"].dropna().astype(str)) - results_clusters
                if orphan:
                    report.add(
                        WARN,
                        "mapping-cluster-orphan",
                        f"{len(orphan)} mapping cluster_name(s) not in results "
                        "clusterName; the inner merge drops these rows.",
                        _rel(report, mp),
                    )
            if "dataset_version_id" in mdf.columns and summary_dvids:
                orphan_dv = (
                    set(mdf["dataset_version_id"].dropna().astype(str)) - summary_dvids
                )
                if orphan_dv:
                    report.add(
                        WARN,
                        "mapping-dvid-orphan",
                        f"{len(orphan_dv)} mapping dataset_version_id(s) not in the "
                        f"summary; their CSDs lack summary metadata: {sorted(orphan_dv)}",
                        _rel(report, mp),
                    )


# ---------------------------------------------------------------------------
# Multi-dataset membership invariant (P1)
# ---------------------------------------------------------------------------


def check_multidataset_mapping(report, nsforest_paths):
    """A results file spanning >1 dataset must resolve every large cluster's
    membership via its cluster_cid_mapping.

    Mirrors the hard-fail invariant NSForestTupleWriter enforces: a
    multi-dataset results file (in prod, only Jorstad) carries a reference
    mapping with a dataset_version_id column that resolves each cluster to one
    of the summary's datasets, so is_about edges come from a single dataset
    rather than fanning out from all.  Single-dataset files need no mapping.
    Surfaced here (ERROR) so the violation is caught at validation time rather
    than as a mid-run exception.
    """
    data_dir = report.data_dir
    for p in nsforest_paths:
        summary_dvids = set()
        for sp in _companions(data_dir, p.name, spec.SUMMARY_PREFIX):
            try:
                sdf = pd.read_csv(sp)
            except Exception:
                continue
            if "dataset_version_id" in sdf.columns:
                summary_dvids |= set(sdf["dataset_version_id"].dropna().astype(str))
        if len(summary_dvids) <= 1:
            continue  # single-dataset: the lone dvid is the only member

        mappings = _companions(data_dir, p.name, spec.MAPPING_PREFIX)
        if not mappings:
            report.add(
                ERROR,
                "multidataset-no-mapping",
                f"results file spans {len(summary_dvids)} datasets but has no "
                "cluster_cid_mapping to resolve per-cluster membership.",
                _rel(report, p),
            )
            continue
        try:
            mdf = pd.read_csv(mappings[0])
        except Exception as exc:
            report.add(ERROR, "unreadable", f"mapping unreadable: {exc}", _rel(report, mappings[0]))
            continue
        if "dataset_version_id" not in mdf.columns:
            report.add(
                ERROR,
                "multidataset-mapping-no-dvid",
                "cluster_cid_mapping for a multi-dataset results file has no "
                "dataset_version_id column.",
                _rel(report, mappings[0]),
            )
            continue
        if "cluster_name" not in mdf.columns:
            report.add(
                ERROR,
                "multidataset-mapping-no-cluster-name",
                "cluster_cid_mapping for a multi-dataset results file has no "
                "cluster_name column.",
                _rel(report, mappings[0]),
            )
            continue

        cmap = dict(
            zip(mdf["cluster_name"].astype(str), mdf["dataset_version_id"].astype(str))
        )
        try:
            rdf = pd.read_csv(p)
        except Exception:
            continue
        if not {"clusterName", "clusterSize"} <= set(rdf.columns):
            continue  # required-column check already reported the gap

        sizes = pd.to_numeric(rdf["clusterSize"], errors="coerce")
        large = rdf.loc[(sizes >= spec.MIN_CLUSTER_SIZE).fillna(False), "clusterName"]
        unresolved = [c for c in large.astype(str) if cmap.get(c) not in summary_dvids]
        if unresolved:
            report.add(
                ERROR,
                "multidataset-cluster-unresolved",
                f"{len(unresolved)} large cluster(s) are not resolved to a summary "
                "dataset_version_id by the mapping; is_about would be ambiguous.",
                _rel(report, p),
            )


# ---------------------------------------------------------------------------
# Driver / reporting
# ---------------------------------------------------------------------------


def validate(data_dir):
    """Run all checks against *data_dir* and return a :class:`Report`."""
    report = Report(data_dir=Path(data_dir))
    nsforest_paths = check_structure(report)
    check_manifest(report)
    check_content(report, nsforest_paths)
    check_value_formats(report, nsforest_paths)
    check_referential(report, nsforest_paths)
    check_multidataset_mapping(report, nsforest_paths)
    return report


def _print_report(report):
    """Print a human-readable summary grouped by severity."""
    errs, warns = report.errors(), report.warnings()
    for f in errs + warns:
        loc = f" [{f.path}]" if f.path else ""
        print(f"{f.severity:5} {f.check}: {f.message}{loc}")
    print(f"\n{len(errs)} error(s), {len(warns)} warning(s) in {report.data_dir}")


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Validate the nlm-ckn data/prod tree against ETL assumptions."
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Path to the nested prod data tree (e.g. .../nlm-ckn/data/prod).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit findings as JSON to stdout."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings as well as errors.",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        parser.error(f"--data-dir not a directory: {data_dir}")

    report = validate(data_dir)

    if args.json:
        print(
            json.dumps(
                {
                    "data_dir": str(report.data_dir),
                    "findings": [vars(f) for f in report.findings],
                    "errors": len(report.errors()),
                    "warnings": len(report.warnings()),
                },
                indent=2,
            )
        )
    else:
        _print_report(report)

    failed = bool(report.errors()) or (args.strict and bool(report.warnings()))
    return 1 if failed else 0


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    sys.exit(main())
