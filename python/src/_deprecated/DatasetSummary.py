import numpy as np
from pathlib import Path

import pandas as pd

DATA_DIRPATH = Path(__file__).resolve().parents[2] / "data"


def summarize_silhouette_fscore(tissue: str, results_dir: Path = None) -> None:
    """Find all silhouette/f-score summary CSVs for a tissue, compute the median
    of (median * f_score) per file, and print a summary.

    Parameters
    ----------
    tissue : str
        Tissue name matching a subdirectory of the results directory (e.g. "liver").
    results_dir : Path, optional
        Path to the top-level results directory. Defaults to the project's
        ``results-6253d09e2fc7`` directory.
    """
    if results_dir is None:
        results_dir = DATA_DIRPATH / "results-6253d09e2fc7"

    tissue_dir = results_dir / tissue
    csv_files = sorted(tissue_dir.rglob("*_silhouette_fscore_summary.csv"))

    if not csv_files:
        print(f"No silhouette/f-score summary files found for tissue '{tissue}'.")
        return

    rows = []
    for path in csv_files:
        dataset = pd.read_csv(path)
        dataset["scaled_median"] = (dataset["median"] + 1) / 2
        dataset["cluster_score"] = np.sqrt(
            dataset["scaled_median"] * dataset["f_score"]
        )
        dataset_score = dataset["cluster_score"].median()
        rows.append(
            {
                "file": path.name,
                "dataset_score": dataset_score,
                "n_cell_types": len(dataset),
            }
        )

    datasets = pd.DataFrame(rows).sort_values("dataset_score", ascending=False)
    datasets.index = range(1, len(datasets) + 1)
    datasets_median = datasets["n_cell_types"].median()
    datasets_iqr = datasets["n_cell_types"].quantile(0.75) - datasets[
        "n_cell_types"
    ].quantile(0.25)
    datasets["median_deviation"] = (
        2 * (datasets["n_cell_types"] - datasets_median) / datasets_iqr
    )

    print(f"\nDataset score for tissue: {tissue}")
    print(f"Files found: {len(csv_files)}")
    print(f"Number of clusters median: {datasets_median}")
    print(f"Number of clusters IQR: {datasets_iqr}\n")
    print(datasets.to_string())

    datasets.to_csv(f"{tissue}.csv")


if __name__ == "__main__":
    import sys

    tissue = sys.argv[1] if len(sys.argv) > 1 else "liver"
    summarize_silhouette_fscore(tissue)
