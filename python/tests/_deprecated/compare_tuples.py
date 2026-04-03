"""Compare tuples produced by old and new tuple writers.

Runs both old and new writers for the first MVP dataset (li-2023),
then compares the tuple sets.
"""

import json
from collections import Counter
from pathlib import Path

import pandas as pd

from LoaderUtilities import (
    DATA_DIRPATH,
    PURLBASE,
    RDFSBASE,
    get_cellxgene_harvester_data,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_results_sources,
    load_results,
)

TUPLES_DIR = DATA_DIRPATH / "tuples"
OLD_TUPLES_DIR = DATA_DIRPATH / "tuples-old"
OLD_TUPLES_DIR.mkdir(parents=True, exist_ok=True)


def extract_predicate(t):
    """Extract the short predicate name from a tuple."""
    pred = t[1] if len(t) >= 3 else ""
    pred = pred.replace(PURLBASE + "/", "").replace(RDFSBASE + "#", "#")
    pred = pred.replace(RDFSBASE + "/", "/")
    return pred


def classify_tuple(t):
    """Classify a tuple as relationship, source, annotation, or edge_annotation."""
    if len(t) == 3:
        if "#" in t[1]:
            return "vertex_annotation"
        return "relationship"
    elif len(t) == 5:
        if t[3].endswith("#Source"):
            return "source"
        return "edge_annotation"
    return "unknown"


def summarize_tuples(tuples, label):
    """Print summary statistics for a set of tuples."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Total tuples: {len(tuples)}")

    by_type = Counter(classify_tuple(t) for t in tuples)
    for k in sorted(by_type):
        print(f"    {k}: {by_type[k]}")

    # Count unique predicates for relationship triples
    rel_preds = Counter()
    ann_attrs = Counter()
    edge_attrs = Counter()
    for t in tuples:
        cls = classify_tuple(t)
        if cls == "relationship":
            rel_preds[extract_predicate(t)] += 1
        elif cls == "vertex_annotation":
            ann_attrs[extract_predicate(t)] += 1
        elif cls == "edge_annotation":
            edge_attrs[extract_predicate(t)] += 1

    print(f"\n  Relationship predicates ({len(rel_preds)} unique):")
    for pred, count in rel_preds.most_common():
        print(f"    {pred}: {count}")

    print(f"\n  Vertex annotation attributes ({len(ann_attrs)} unique):")
    for attr, count in ann_attrs.most_common():
        print(f"    {attr}: {count}")

    if edge_attrs:
        print(f"\n  Edge annotation attributes ({len(edge_attrs)} unique):")
        for attr, count in edge_attrs.most_common():
            print(f"    {attr}: {count}")


def diff_tuples(old_tuples, new_tuples, label):
    """Compare two tuple sets and report differences."""
    # Normalize to comparable form (lists → tuples)
    old_set = set(tuple(t) for t in old_tuples)
    new_set = set(tuple(t) for t in new_tuples)

    only_old = old_set - new_set
    only_new = new_set - old_set
    common = old_set & new_set

    print(f"\n{'=' * 60}")
    print(f"  DIFF: {label}")
    print(f"{'=' * 60}")
    print(f"  Common tuples: {len(common)}")
    print(f"  Only in OLD:   {len(only_old)}")
    print(f"  Only in NEW:   {len(only_new)}")

    if only_old:
        print(f"\n  Sample tuples only in OLD (up to 10):")
        old_by_type = Counter(classify_tuple(t) for t in only_old)
        for k in sorted(old_by_type):
            print(f"    {k}: {old_by_type[k]}")
        for t in sorted(only_old)[:10]:
            short = [s.replace(PURLBASE + "/", "").replace(RDFSBASE, "RDFS") for s in t]
            print(f"    {short}")

    if only_new:
        print(f"\n  Sample tuples only in NEW (up to 10):")
        new_by_type = Counter(classify_tuple(t) for t in only_new)
        for k in sorted(new_by_type):
            print(f"    {k}: {new_by_type[k]}")
        for t in sorted(only_new)[:10]:
            short = [s.replace(PURLBASE + "/", "").replace(RDFSBASE, "RDFS") for s in t]
            print(f"    {short}")


def run_old_nsforest(nsforest_path, scores_path, summary_path, dvids):
    """Run old NSForest tuple writer for one dataset."""
    from ExternalApiResultsFetcher import CELLXGENE_PATH
    from _deprecated.NSForestResultsTupleWriter import create_tuples_from_nsforest

    nsforest_results = load_results(nsforest_path).sort_values(
        "clusterName", ignore_index=True
    )
    if scores_path:
        cluster_header = nsforest_results.loc[0, "cluster_header"]
        silhouette_scores = load_results(scores_path[0]).sort_values(
            cluster_header, ignore_index=True
        )
        nsforest_results = nsforest_results.merge(
            silhouette_scores[[cluster_header, "median"]].copy(),
            left_on="clusterName",
            right_on=cluster_header,
        )
    summary_data = load_results(summary_path[0]) if summary_path else pd.DataFrame()
    with open(CELLXGENE_PATH, "r") as fp:
        cellxgene_results = json.load(fp)
    return create_tuples_from_nsforest(
        nsforest_results, summary_data, dvids, cellxgene_results
    )


def run_old_mapping(nsforest_path, mapping_path, dvids):
    """Run old AuthorToCl tuple writer for one dataset."""
    from ExternalApiResultsFetcher import CELLXGENE_PATH
    from _deprecated.AuthorToClResultsTupleWriter import create_tuples_from_author_to_cl

    nsforest_results = load_results(nsforest_path).sort_values(
        "clusterName", ignore_index=True
    )
    author_to_cl_results = (
        load_results(mapping_path[0])
        .sort_values("author_cell_set", ignore_index=True)
        .drop(columns=["uuid"])
    )
    author_to_cl_results = author_to_cl_results.merge(
        nsforest_results[
            ["clusterName", "clusterSize", "NSForest_markers", "binary_genes", "uuid"]
        ].copy(),
        left_on="author_cell_set",
        right_on="clusterName",
    )
    with open(CELLXGENE_PATH, "r") as fp:
        cellxgene_results = json.load(fp)
    return create_tuples_from_author_to_cl(
        author_to_cl_results, dvids, cellxgene_results
    )


def run_new_nsforest(nsforest_path, scores_path, summary_path, dvids, harvester_data):
    """Run new NSForest tuple writer for one dataset."""
    from NSForestTupleWriter import create_tuples

    nsforest_results = load_results(nsforest_path).sort_values(
        "clusterName", ignore_index=True
    )
    if scores_path:
        cluster_header = nsforest_results.loc[0, "cluster_header"]
        silhouette_scores = load_results(scores_path[0]).sort_values(
            cluster_header, ignore_index=True
        )
        nsforest_results = nsforest_results.merge(
            silhouette_scores[[cluster_header, "median"]].copy(),
            left_on="clusterName",
            right_on=cluster_header,
        )
    summary_data = load_results(summary_path[0]) if summary_path else pd.DataFrame()
    return create_tuples(nsforest_results, summary_data, dvids, harvester_data)


def run_new_mapping(nsforest_path, mapping_path, dvids, harvester_data):
    """Run new Mapping tuple writer for one dataset."""
    from MappingTupleWriter import create_tuples

    nsforest_results = load_results(nsforest_path).sort_values(
        "clusterName", ignore_index=True
    )
    author_to_cl_results = (
        load_results(mapping_path[0])
        .sort_values("author_cell_set", ignore_index=True)
        .drop(columns=["uuid"])
    )
    author_to_cl_results = author_to_cl_results.merge(
        nsforest_results[
            ["clusterName", "clusterSize", "NSForest_markers", "binary_genes", "uuid"]
        ].copy(),
        left_on="author_cell_set",
        right_on="clusterName",
    )
    return create_tuples(author_to_cl_results, dvids, harvester_data)


def main():
    results_sources = get_results_sources()
    harvester_data = get_cellxgene_harvester_data(results_sources)
    file_paths = get_dataset_file_paths(results_sources)
    dvid_lists = get_dataset_version_id_lists(file_paths)

    # Use first dataset with a mapping file
    for i, mapping_path in enumerate(file_paths["mapping_paths"]):
        if mapping_path:
            break

    nsforest_path = file_paths["nsforest_paths"][i]
    mapping_path = file_paths["mapping_paths"][i]
    scores_path = file_paths["scores_paths"][i]
    summary_path = file_paths["summary_paths"][i]
    dvids = dvid_lists[i]

    print(f"Comparing for dataset: {nsforest_path.name}")
    print(f"  Mapping: {mapping_path[0].name}")
    print(f"  DVIDs: {dvids}")

    # --- NSForest comparison ---
    old_nsf = run_old_nsforest(nsforest_path, scores_path, summary_path, dvids)
    new_nsf = run_new_nsforest(nsforest_path, scores_path, summary_path, dvids, harvester_data)

    summarize_tuples(old_nsf, "OLD NSForest")
    summarize_tuples(new_nsf, "NEW NSForest")
    diff_tuples(old_nsf, new_nsf, "NSForest")

    # --- Mapping comparison ---
    old_map = run_old_mapping(nsforest_path, mapping_path, dvids)
    new_map = run_new_mapping(nsforest_path, mapping_path, dvids, harvester_data)

    summarize_tuples(old_map, "OLD Mapping")
    summarize_tuples(new_map, "NEW Mapping")
    diff_tuples(old_map, new_map, "Mapping")

    # --- Combined comparison ---
    old_all = old_nsf + old_map
    new_all = new_nsf + new_map
    diff_tuples(old_all, new_all, "NSForest + Mapping Combined")

    # --- External API comparisons ---
    compare_external_api()


def compare_external_api():
    """Compare old and new external API tuple writers."""
    import json
    from glob import glob

    from ExternalApiResultsFetcher import (
        CELLXGENE_PATH,
        OPENTARGETS_PATH,
        OPENTARGETS_RESOURCES,
        GENE_PATH,
        UNIPROT_PATH,
        HUBMAP_DIRPATH,
    )
    from _deprecated.ExternalApiResultsTupleWriter import (
        create_tuples_from_cellxgene,
        create_tuples_from_opentargets,
        create_tuples_from_gene,
        create_tuples_from_uniprot,
        create_tuples_from_hubmap,
    )
    from CellxGeneTupleWriter import create_tuples as new_cellxgene
    from OpenTargetsTupleWriter import create_tuples as new_opentargets
    from GeneTupleWriter import create_tuples as new_gene
    from UniProtTupleWriter import create_tuples as new_uniprot
    from HuBMAPTupleWriter import create_tuples as new_hubmap

    # Load shared data
    with open(CELLXGENE_PATH, "r") as fp:
        cellxgene_results = json.load(fp)
    with open(OPENTARGETS_PATH, "r") as fp:
        opentargets_results = json.load(fp)
    with open(GENE_PATH, "r") as fp:
        gene_results = json.load(fp)
    with open(UNIPROT_PATH, "r") as fp:
        uniprot_results = json.load(fp)

    # --- CELLxGENE ---
    print("\n\n" + "#" * 70)
    print("# CELLxGENE COMPARISON")
    print("#" * 70)
    old_cxg, _ = create_tuples_from_cellxgene(cellxgene_results)
    new_cxg = new_cellxgene(cellxgene_results)
    summarize_tuples(old_cxg, "OLD CELLxGENE")
    summarize_tuples(new_cxg, "NEW CELLxGENE")
    diff_tuples(old_cxg, new_cxg, "CELLxGENE")

    # --- OpenTargets ---
    print("\n\n" + "#" * 70)
    print("# OPEN TARGETS COMPARISON")
    print("#" * 70)
    old_ot, _ = create_tuples_from_opentargets(opentargets_results, gene_results)
    new_ot = new_opentargets(opentargets_results, gene_results)
    summarize_tuples(old_ot, "OLD Open Targets")
    summarize_tuples(new_ot, "NEW Open Targets")
    diff_tuples(old_ot, new_ot, "Open Targets")

    # --- Gene ---
    print("\n\n" + "#" * 70)
    print("# GENE COMPARISON")
    print("#" * 70)
    # Reload gene_results since old writer may mutate it
    with open(GENE_PATH, "r") as fp:
        gene_results_fresh = json.load(fp)
    old_gene, _ = create_tuples_from_gene(gene_results_fresh)
    with open(GENE_PATH, "r") as fp:
        gene_results_fresh2 = json.load(fp)
    new_gene_tuples = new_gene(gene_results_fresh2)
    summarize_tuples(old_gene, "OLD Gene")
    summarize_tuples(new_gene_tuples, "NEW Gene")
    diff_tuples(old_gene, new_gene_tuples, "Gene")

    # --- UniProt ---
    print("\n\n" + "#" * 70)
    print("# UNIPROT COMPARISON")
    print("#" * 70)
    old_uni, _ = create_tuples_from_uniprot(uniprot_results)
    new_uni = new_uniprot(uniprot_results)
    summarize_tuples(old_uni, "OLD UniProt")
    summarize_tuples(new_uni, "NEW UniProt")
    diff_tuples(old_uni, new_uni, "UniProt")

    # --- HuBMAP ---
    print("\n\n" + "#" * 70)
    print("# HUBMAP COMPARISON")
    print("#" * 70)
    results_sources = get_results_sources()
    file_paths = get_dataset_file_paths(results_sources)
    from LoaderUtilities import get_cl_terms
    cl_terms = get_cl_terms(file_paths["mapping_paths"])

    hubmap_paths = sorted(Path(p).resolve() for p in glob(str(HUBMAP_DIRPATH / "*.json")))
    if hubmap_paths:
        hubmap_path = hubmap_paths[0]  # Compare first HuBMAP file
        print(f"Comparing HuBMAP file: {hubmap_path.name}")
        with open(hubmap_path, "r") as fp:
            hubmap_data = json.load(fp)
        old_hub, _ = create_tuples_from_hubmap(hubmap_data, cl_terms)
        with open(hubmap_path, "r") as fp:
            hubmap_data2 = json.load(fp)
        new_hub = new_hubmap(hubmap_data2, cl_terms)
        summarize_tuples(old_hub, f"OLD HuBMAP ({hubmap_path.name})")
        summarize_tuples(new_hub, f"NEW HuBMAP ({hubmap_path.name})")
        diff_tuples(old_hub, new_hub, f"HuBMAP ({hubmap_path.name})")


if __name__ == "__main__":
    main()
