import ast
import json
from pathlib import Path

from rdflib.term import Literal, URIRef

from ExternalApiResultsFetcher import CELLXGENE_PATH
from LoaderUtilities import (
    MIN_CLUSTER_SIZE,
    PURLBASE,
    RDFSBASE,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_results_sources,
    hyphenate,
    load_results,
)

TUPLES_DIRPATH = Path(__file__).parents[2] / "data" / "tuples"


def create_tuples_from_nsforest(
    nsforest_results, dataset_version_ids, cellxgene_results
):
    """Creates tuples from NSForest results consistent with schema
    v0.7. Exclude clusters smaller than the minimum size.

    Parameters
    ----------
    nsforest_results : pd.DataFrame
        DataFrame containing NSForest results
    dataset_version_ids: list(str)
        List of the dataset version identifiers corresponding to the
        datasets used to generate the NSForest results
    cellxgene_results : dict
        Dictionaries containing cellxgene results dictionaries keyed
        by dataset_version_id

    Returns
    -------
    tuples : list(tuple(str))
        List of tuples (triples or quadruples) created
    """
    tuples = []

    # Nodes for each cell set, marker and binary genes, and cell type
    for _, row in nsforest_results.iterrows():
        uuid = row["uuid"]
        cluster_name = hyphenate(row["clusterName"])
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue
        if "silhouette_score" in row:
            silhouette_score = row["median"]
        else:
            silhouette_score = None
        binary_genes = ast.literal_eval(row["binary_genes"])
        nsforest_markers = ast.literal_eval(row["NSForest_markers"])
        cs_term = f"CS_{cluster_name}-{uuid}"
        bmc_term = f"BMC_{uuid}"
        bgs_term = f"BGS_{uuid}"

        # Biomarker_combination_Ind, INSTANCE_OF, Biomarker_combination_Class
        # ---, rdf:type, SO:0001260
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{bmc_term}"),
                URIRef(f"{RDFSBASE}/rdf#type"),
                URIRef(f"{PURLBASE}/SO_0001260"),
            )
        )
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{bmc_term}"),
                URIRef(f"{RDFSBASE}/rdf#type"),
                URIRef(f"{PURLBASE}/SO_0001260"),
                URIRef(f"{RDFSBASE}#Source"),
                Literal("NSForest"),
            )
        )

        # Gene_Class, PART_OF, Biomarker_combination_Ind
        # SO:0000704, BFO:0000050, SO:0001260
        for gene in nsforest_markers:
            gs_term = f"GS_{gene}"
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/BFO_0000050"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                )
            )
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/BFO_0000050"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#Source"),
                    Literal("NSForest"),
                )
            )

        # Cell_set_Ind, HAS_CHARACTERIZING_MARKER_SET, Biomarker_combination_Ind
        # ---, RO:0015004, SO:0001260
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{PURLBASE}/RO_0015004"),
                URIRef(f"{PURLBASE}/{bmc_term}"),
            )
        )
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{PURLBASE}/RO_0015004"),
                URIRef(f"{PURLBASE}/{bmc_term}"),
                URIRef(f"{RDFSBASE}#Source"),
                Literal("NSForest"),
            )
        )

        # Biomarker_combination_Ind, SUBCLUSTER_OF, Binary_gene_combination_Ind
        # SO:0001260, RO:0015003, SO:0001260
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{bmc_term}"),
                URIRef(f"{PURLBASE}/RO_0015003"),
                URIRef(f"{PURLBASE}/{bgs_term}"),
            )
        )
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{bmc_term}"),
                URIRef(f"{PURLBASE}/RO_0015003"),
                URIRef(f"{PURLBASE}/{bgs_term}"),
                URIRef(f"{RDFSBASE}#Source"),
                Literal("NSForest"),
            )
        )

        # Node annotations

        # Cell_set_Ind, STATO:0000047 (count), clusterSize
        # Cell_set_Ind, -, binary_genes
        # Cell_set_Ind, RO:0015004 (has characterizing marker set), NSForest_markers
        tuples.extend(
            [
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}#F_beta_confidence_score"),  # [STAT:0000663]
                    Literal(str(row["f_score"])),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}#Total_cell_count"),
                    Literal(str(cluster_size)),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}#Binary_genes"),
                    Literal(" ".join(binary_genes)),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}#Markers"),
                    Literal(" ".join(nsforest_markers)),
                ),
            ]
        )
        if silhouette_score:
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}#Silhouette_score"),
                    Literal(str(silhouette_score)),
                ),
            )

        # Binary_gene_set, -, binary_genes
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{bgs_term}"),
                URIRef(f"{RDFSBASE}#Binary_genes"),
                Literal(" ".join(binary_genes)),
            )
        )

        # Biomarker_combination_Ind, RO:0015004 (has characterizing marker set), NSForest_markers
        tuples.extend(
            [
                (
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#Markers"),
                    Literal(" ".join(nsforest_markers)),
                ),
                (
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#F_beta_confidence_score"),  # [STAT:0000663]
                    Literal(str(row["f_score"])),
                ),
            ]
        )

        # Edge annotations for BMC terms
        tuples.extend(
            [
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{PURLBASE}/#source_algorithm"),  # [IAO_0000064]
                    Literal("NSForest-v4.0_dev"),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#F_beta_confidence_score"),  # [STAT:0000663]
                    Literal(str(row["f_score"])),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#Precision"),  # [STAT:0000416]
                    Literal(str(row["precision"])),
                ),
            ]
        )
        # TODO: Restore when available in data
        # tuples.append(
        #     (
        #         URIRef(f"{PURLBASE}/{cs_term}"),
        #         URIRef(f"{PURLBASE}/RO_0015004"),
        #         URIRef(f"{PURLBASE}/{bmc_term}"),
        #         URIRef(f"{RDFSBASE}#Recall"),  # [STAT:0000233]
        #         Literal(str(row["recall"])),
        #     )
        # )
        tuples.extend(
            [
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#TN"),  # [STAT:0000597]
                    Literal(str(row["TN"])),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{RDFSBASE}#TP"),  # [STAT:0000595]
                    Literal(str(row["TP"])),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#FN"),  # [STAT:0000598]
                    Literal(str(row["FN"])),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#FP"),  # [STAT:0000596]
                    Literal(str(row["FP"])),
                ),
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{PURLBASE}/RO_0015004"),
                    URIRef(f"{PURLBASE}/{bmc_term}"),
                    URIRef(f"{RDFSBASE}#Marker_count"),  # [STAT:0000047]
                    Literal(str(row["marker_count"])),
                ),
            ]
        )

        # TODO: Determine on which edge this attribute belongs
        # Edge annotations for BGC terms
        # tuples.append(
        #     (
        #         URIRef(f"{PURLBASE}/{cs_term}"),
        #         URIRef(f"{PURLBASE}/{bgs_term}"),
        #         URIRef(f"{RDFSBASE}#On_target"),  # [STAT:0000047]
        #         Literal(str(row["onTarget"])),
        #     )
        # )

        for dataset_version_id in dataset_version_ids:
            csd_term = f"CSD_{dataset_version_id}"

            # Cell_set_Ind, SOURCE, Cell_set_dataset_Ind
            # -, dc:source, IAO:0000100
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}/dc#Source"),
                    URIRef(f"{PURLBASE}/{csd_term}"),
                )
            )
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cs_term}"),
                    URIRef(f"{RDFSBASE}/dc#Source"),
                    URIRef(f"{PURLBASE}/{csd_term}"),
                    URIRef(f"{RDFSBASE}#Source"),
                    Literal("NSForest"),
                )
            )

    return tuples


def main(summarize=False):
    """Get results sources directories and patterns, all NSForest results, and
    mapping, silhouette scores, and dataset summary file paths, and dataset
    version id lists in order to create tuples consistent with schema v0.7, and
    write the result to a JSON file. If summarizing, retain the first row only,
    and include results in output.

    Parameters
    ----------
    summarize : bool
        Flag to summarize results, or not

    Returns
    -------
    None
    """
    # Get results sources directories and patterns, all NSForest results, and
    # mapping, silhouette scores, and dataset summary file paths, and dataset
    # version id lists, and load CELLxGENE data
    results_sources = get_results_sources()
    file_paths = get_dataset_file_paths(results_sources)
    nsforest_paths = file_paths["nsforest_paths"]
    scores_path = file_paths["scores_paths"]
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)
    with open(CELLXGENE_PATH, "r") as fp:
        cellxgene_results = json.load(fp)
    for nsforest_path, score_path, dataset_version_ids in zip(
        nsforest_paths, scores_path, dataset_version_id_lists
    ):
        # Load NSForest results
        nsforest_results = load_results(nsforest_path).sort_values(
            "clusterName", ignore_index=True
        )
        if summarize:
            nsforest_results = nsforest_results.head(1)

        if score_path != []:
            # Load silhouette scores
            cluster_header = nsforest_results.loc[0, "cluster_header"]
            silhouette_scores = load_results(score_path[0]).sort_values(
                cluster_header, ignore_index=True
            )

            # Merge silhouette scores with NSForest results since author
            # cell sets may not align exactly
            nsforest_results = nsforest_results.merge(
                silhouette_scores[[cluster_header, "median"]].copy(),
                left_on="clusterName",
                right_on=cluster_header,
            )

        print(f"Creating tuples from {nsforest_path}")
        nsforest_tuples = create_tuples_from_nsforest(
            nsforest_results, dataset_version_ids, cellxgene_results
        )
        if summarize:
            output_dirpath = TUPLES_DIRPATH / "summaries"
        else:
            output_dirpath = TUPLES_DIRPATH
        with open(
            output_dirpath / nsforest_path.name.replace(".csv", ".json"), "w"
        ) as f:
            data = {}
            if summarize:
                data["results"] = nsforest_results.to_dict()
            data["tuples"] = nsforest_tuples
            json.dump(data, f, indent=4)

        if summarize:
            break


if __name__ == "__main__":
    main(summarize=True)
    main()
