"""Compare old ExternalApiResultsFetcher output with new
ExternalApiDataFetcher + ExternalApiDataTransformer output.

The old output (from the original fetcher) lives in one directory,
and the new output (from the refactored fetcher) lives in another.
The transformer is applied to the new raw data before comparison.

Usage:
    python compare_fetcher_outputs.py --old-dir data/external-test
    python compare_fetcher_outputs.py --old-dir data/external-test --source cellxgene
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from LoaderUtilities import EXTERNAL_DIRPATH

from DataTransformer import (
    CellxGeneTransformer,
    GeneTransformer,
    OpenTargetsTransformer,
    UniProtTransformer,
)

# Sources to compare: (name, output filename, transformer class)
COMPARABLE_SOURCES = [
    ("cellxgene", "cellxgene.json", CellxGeneTransformer),
    ("opentargets", "opentargets.json", OpenTargetsTransformer),
    ("gene", "gene.json", GeneTransformer),
    ("uniprot", "uniprot.json", UniProtTransformer),
]

# Top-level keys that hold metadata lists (not actual results).
# These are sorted before comparison since insertion order may vary.
METADATA_KEYS = {
    "protein_accessions",
    "gene_entrez_ids",
    "gene_ensembl_ids",
}


def deep_compare(old, new, path=""):
    """Recursively compare two objects and return a list of differences.

    Parameters
    ----------
    old : object
        Value from the old output
    new : object
        Value from the new (transformed) output
    path : str
        Dot-separated key path for reporting

    Returns
    -------
    list
        List of difference description strings
    """
    diffs = []

    if type(old) != type(new):
        diffs.append(
            f"  {path}: type mismatch: {type(old).__name__} vs {type(new).__name__}"
        )
        return diffs

    if isinstance(old, dict):
        old_keys = set(old.keys())
        new_keys = set(new.keys())
        for key in sorted(old_keys - new_keys):
            diffs.append(f"  {path}.{key}: missing in new")
        for key in sorted(new_keys - old_keys):
            diffs.append(f"  {path}.{key}: extra in new")
        for key in sorted(old_keys & new_keys):
            diffs.extend(deep_compare(old[key], new[key], f"{path}.{key}"))

    elif isinstance(old, list):
        if len(old) != len(new):
            diffs.append(f"  {path}: list length mismatch: {len(old)} vs {len(new)}")
        for i in range(min(len(old), len(new))):
            diffs.extend(deep_compare(old[i], new[i], f"{path}[{i}]"))

    else:
        if old != new:
            old_str = repr(old)[:80]
            new_str = repr(new)[:80]
            diffs.append(f"  {path}: {old_str} vs {new_str}")

    return diffs


def compare_source(
    name, filename, transformer_class, old_dir, new_dir, max_diffs_per_key=5
):
    """Compare old output with new transformer output for a single
    source.

    Parameters
    ----------
    name : str
        Source name for display
    filename : str
        JSON filename in both directories
    transformer_class : type
        The transformer class to instantiate
    old_dir : Path
        Directory containing old (original fetcher) output
    new_dir : Path
        Directory containing new (refactored fetcher) raw output
    max_diffs_per_key : int
        Max differences to show per top-level key

    Returns
    -------
    bool
        True if outputs match, False otherwise
    """
    old_path = old_dir / filename
    new_path = new_dir / filename

    if not old_path.exists():
        print(f"[{name}] No old output found at {old_path}")
        return False

    if not new_path.exists():
        print(f"[{name}] No new output found at {new_path}")
        return False

    print(f"[{name}] Loading old output from {old_path}")
    with open(old_path, "r") as fp:
        old_results = json.load(fp)

    print(f"[{name}] Transforming new output from {new_path}")
    transformer = transformer_class()
    transformer.input_path = new_path
    new_results = transformer.run()

    # Sort metadata lists before comparison
    for key in METADATA_KEYS:
        if key in old_results and isinstance(old_results[key], list):
            old_results[key] = sorted(old_results[key])
        if key in new_results and isinstance(new_results[key], list):
            new_results[key] = sorted(new_results[key])

    # Compare top-level keys
    old_keys = set(old_results.keys())
    new_keys = set(new_results.keys())

    missing = old_keys - new_keys
    extra = new_keys - old_keys
    common = old_keys & new_keys

    total_diffs = 0
    if missing:
        print(f"[{name}] Keys in old but not new ({len(missing)}):")
        for k in sorted(list(missing)[:10]):
            print(f"  - {k}")
        total_diffs += len(missing)

    if extra:
        print(f"[{name}] Keys in new but not old ({len(extra)}):")
        for k in sorted(list(extra)[:10]):
            print(f"  + {k}")
        total_diffs += len(extra)

    # Compare common keys
    keys_with_diffs = 0
    for key in sorted(common):
        diffs = deep_compare(old_results[key], new_results[key], key)
        if diffs:
            keys_with_diffs += 1
            total_diffs += len(diffs)
            print(f"[{name}] Differences for key '{key}':")
            for diff in diffs[:max_diffs_per_key]:
                print(diff)
            if len(diffs) > max_diffs_per_key:
                print(f"  ... and {len(diffs) - max_diffs_per_key} more differences")

    if total_diffs == 0:
        print(f"[{name}] MATCH — {len(common)} entries identical")
        return True
    else:
        print(
            f"[{name}] MISMATCH — {total_diffs} differences across "
            f"{keys_with_diffs} keys (of {len(common)} common keys)"
        )
        return False


def compare_all(old_dir, new_dir, source_filter=None):
    """Run comparison for all (or filtered) sources.

    Parameters
    ----------
    old_dir : Path
        Directory containing old (original fetcher) output
    new_dir : Path
        Directory containing new (refactored fetcher) raw output
    source_filter : str or None
        If provided, only compare this source name

    Returns
    -------
    dict
        Mapping of source name to match result (bool)
    """
    results = {}
    for name, filename, transformer_class in COMPARABLE_SOURCES:
        if source_filter and name != source_filter:
            continue
        print(f"\n{'=' * 60}")
        print(f"Comparing: {name}")
        print(f"{'=' * 60}")
        results[name] = compare_source(
            name, filename, transformer_class, old_dir, new_dir
        )

    print(f"\n{'=' * 60}")
    print("Summary")
    print(f"{'=' * 60}")
    for name, matched in results.items():
        status = "MATCH" if matched else "MISMATCH"
        print(f"  {name}: {status}")

    return results


def main():
    """CLI for comparing old and new fetcher outputs."""
    parser = argparse.ArgumentParser(description="Compare old and new fetcher outputs")
    parser.add_argument(
        "--old-dir",
        type=Path,
        required=True,
        help="directory containing old (original fetcher) output",
    )
    parser.add_argument(
        "--new-dir",
        type=Path,
        default=EXTERNAL_DIRPATH,
        help=f"directory containing new (refactored fetcher) output "
        f"(default: {EXTERNAL_DIRPATH})",
    )
    parser.add_argument(
        "--source",
        choices=[name for name, _, _ in COMPARABLE_SOURCES],
        help="compare only this source",
    )
    args = parser.parse_args()

    results = compare_all(
        old_dir=args.old_dir,
        new_dir=args.new_dir,
        source_filter=args.source,
    )
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
