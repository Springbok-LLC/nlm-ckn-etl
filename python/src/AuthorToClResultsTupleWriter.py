import ast
import json
from pathlib import Path
from urllib.parse import urlparse

from rdflib.term import Literal, URIRef

from ExternalApiResultsFetcher import CELLXGENE_PATH
from LoaderUtilities import (
    DEPRECATED_TERMS,
    MIN_CLUSTER_SIZE,
    PURLBASE,
    RDFSBASE,
    get_results_sources,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    load_results,
    hyphenate,
)

TUPLES_DIRPATH = Path(__file__).parents[2] / "data" / "tuples"


def create_tuples_from_author_to_cl(
    author_to_cl_results, dataset_version_ids, cellxgene_results
):
    """Creates tuples from manual author cell set to CL term mapping
    consistent with schema v0.7. Exclude clusters smaller than the
    minimum size. Create a cell set dataset for "--" separated lists
    of cell set datasets.

    Parameters
    ----------
    author_to_cl_results : pd.DataFrame
        DataFrame containing author to CL results
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

    # Nodes for each cell type or cell set
    for _, row in author_to_cl_results.iterrows():
        uuid = row["uuid"]
        cl_term = urlparse(row["cell_ontology_id"]).path.replace("/obo/", "")
        if cl_term in DEPRECATED_TERMS:
            print(f"Warning: CL term {cl_term} deprecated")
        uberon_term = urlparse(row["uberon_entity_id"]).path.replace("/obo/", "")
        if uberon_term in DEPRECATED_TERMS:
            print(f"Warning: UBERON term {uberon_term} deprecated")
        author_cell_set = hyphenate(row["author_cell_set"])
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue
        cs_term = f"CS_{author_cell_set}-{uuid}"
        # bmc_term = f"BMC_{uuid}"
        bgs_term = f"BGS_{uuid}"

        # Cell_type_Class, PART_OF, Anatomical_structure_Class
        # CL:0000000, BFO:0000050, UBERON:0001062
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cl_term}"),
                URIRef(f"{PURLBASE}/BFO_0000050"),
                URIRef(f"{PURLBASE}/{uberon_term}"),
            )
        )
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cl_term}"),
                URIRef(f"{PURLBASE}/BFO_0000050"),
                URIRef(f"{PURLBASE}/{uberon_term}"),
                URIRef(f"{RDFSBASE}#Source"),
                Literal("Manual Mapping"),
            )
        )

        for dataset_version_id in dataset_version_ids:
            csd_term = f"CSD_{dataset_version_id}"

            # Cell_type_Class, HAS_EXEMPLAR_DATA, Cell_set_dataset_Ind
            # CL:0000000, RO:0015001, IAO:0000100
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cl_term}"),
                    URIRef(f"{PURLBASE}/RO_0015001"),
                    URIRef(f"{PURLBASE}/{csd_term}"),
                )
            )
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cl_term}"),
                    URIRef(f"{PURLBASE}/RO_0015001"),
                    URIRef(f"{PURLBASE}/{csd_term}"),
                    URIRef(f"{RDFSBASE}#Source"),
                    Literal("Manual Mapping"),
                )
            )

        # Cell_set_Ind, COMPOSED_PRIMARILY_OF, Cell_type_Class
        # -, RO:0002473, CL:0000000
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{PURLBASE}/RO_0002473"),
                URIRef(f"{PURLBASE}/{cl_term}"),
            )
        )
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{PURLBASE}/RO_0002473"),
                URIRef(f"{PURLBASE}/{cl_term}"),
                URIRef(f"{RDFSBASE}#Source"),
                Literal("Manual Mapping"),
            )
        )

        # Biomarker_combination_Ind, IS_CHARACTERIZING_MARKER_SET_FOR, Cell_type_Class
        # TODO: Update and use RO term
        # -, RO:0015004, CL:0000000
        # NOTE: Removed to resolve issue 106
        # tuples.append(
        #     (
        #         URIRef(f"{PURLBASE}/{bmc_term}"),
        #         URIRef(f"{PURLBASE}/RO_0015004"),
        #         URIRef(f"{PURLBASE}/{cl_term}"),
        #     )
        # )
        # tuples.append(
        #     (
        #         URIRef(f"{PURLBASE}/{bmc_term}"),
        #         URIRef(f"{PURLBASE}/RO_0015004"),
        #         URIRef(f"{PURLBASE}/{cl_term}"),
        #         URIRef(f"{RDFSBASE}#Source"),
        #         Literal("Manual Mapping"),
        #     )
        # )

        # Node annotations
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{RDFSBASE}#Cell_type"),
                Literal(cl_term),
            )
        )

        # Edge annotations
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{PURLBASE}/RO_0002473"),
                URIRef(f"{PURLBASE}/{cl_term}"),
                URIRef(f"{RDFSBASE}#Match"),
                Literal(row["match"]),
            )
        )
        tuples.append(
            (
                URIRef(f"{PURLBASE}/{cs_term}"),
                URIRef(f"{PURLBASE}/RO_0002473"),
                URIRef(f"{PURLBASE}/{cl_term}"),
                URIRef(f"{RDFSBASE}#Mapping_method"),
                Literal(row["mapping_method"]),
            )
        )

        # Nodes for each cell type and marker gene
        marker_genes = ast.literal_eval(row["NSForest_markers"])
        for gene in marker_genes:
            gs_term = f"GS_{gene}"

            # Gene_Class, PART_OF, Cell_type_Class
            # SO:0000704, BFO:0000050, CL:0000000
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/BFO_0000050"),
                    URIRef(f"{PURLBASE}/{cl_term}"),
                )
            )
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/BFO_0000050"),
                    URIRef(f"{PURLBASE}/{cl_term}"),
                    URIRef(f"{RDFSBASE}#Source"),
                    Literal("NSForest"),
                )
            )

        # Nodes for each cell type, and marker and binary gene
        binary_genes = ast.literal_eval(row["binary_genes"])
        for gene in marker_genes + binary_genes:
            gs_term = f"GS_{gene}"

            # Cell_type_Class, SELECTIVELY EXPRESS, Gene_Class
            # TODO: Update and use RO term
            # CL:0000000, RO:0002294, SO:0000704
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cl_term}"),
                    URIRef(f"{RDFSBASE}#RO_0002294"),
                    URIRef(f"{PURLBASE}/{gs_term}"),
                )
            )
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{cl_term}"),
                    URIRef(f"{RDFSBASE}#RO_0002294"),
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{RDFSBASE}#Source"),
                    Literal("Manual Mapping"),
                )
            )

            # Gene_Class, PART_OF, Cell_type_Class
            # SO:0000704, BFO:0000050, CL:0000000
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/BFO_0000050"),
                    URIRef(f"{PURLBASE}/{cl_term}"),
                )
            )
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/BFO_0000050"),
                    URIRef(f"{PURLBASE}/{cl_term}"),
                    URIRef(f"{RDFSBASE}#Source"),
                    Literal("NSForest"),
                )
            )

            # Gene_Class, EXPRESSED_IN, Anatomical_structure_Class
            # SO:0000704, RO:0002206, UBERON:0001062
            # NOTE: Removed to resolve issue 105
            # tuples.append(
            #     (
            #         URIRef(f"{PURLBASE}/{gs_term}"),
            #         URIRef(f"{PURLBASE}/RO_0002206"),
            #         URIRef(f"{PURLBASE}/{uberon_term}"),
            #     )
            # )
            # tuples.append(
            #     (
            #         URIRef(f"{PURLBASE}/{gs_term}"),
            #         URIRef(f"{PURLBASE}/RO_0002206"),
            #         URIRef(f"{PURLBASE}/{uberon_term}"),
            #         URIRef(f"{RDFSBASE}#Source"),
            #         Literal("Manual Mapping"),
            #     )
            # )

    return tuples


def main(summarize=False):
    """Get results sources directories and patterns, all NSForest results, and
    mapping, silhouette scores, and dataset summary file paths, CELLxGENE data
    in order to create tuples consistent with schema v0.7, and write the result
    to a JSON file. If summarizing, retain the first row only, and include
    results in output.

    Parameters
    ----------
    summarize : bool
        Flag to summarize results, or not

    Returns
    -------
    None
    """
    # Get results sources directories and patterns, and all NSForest results,
    # and mapping, silhouette scores, and dataset summary file paths, then load
    # CELLxGENE data
    results_sources = get_results_sources()
    file_paths = get_dataset_file_paths(results_sources)
    author_to_cl_paths = file_paths["mapping_paths"]
    nsforest_paths = file_paths["nsforest_paths"]
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)
    with open(CELLXGENE_PATH, "r") as fp:
        cellxgene_results = json.load(fp)
    for author_to_cl_path, nsforest_path, dataset_version_id_list in zip(
        author_to_cl_paths, nsforest_paths, dataset_version_id_lists
    ):
        if author_to_cl_path == []:
            print(
                f"No author cell set to CL term map for NSForest results {nsforest_path}"
            )
            continue
        author_to_cl_path = author_to_cl_path[0]

        # Load author cell set to CL term mapping, dropping "uuid"
        # column in order to merge "uuid" column from NSForest results
        author_to_cl_results = (
            load_results(author_to_cl_path)
            .sort_values("author_cell_set", ignore_index=True)
            .drop(columns=["uuid"])
        )

        # Load NSForest results
        nsforest_results = load_results(nsforest_path).sort_values(
            "clusterName", ignore_index=True
        )
        if summarize:
            # Work around for Li
            nsforest_results = nsforest_results.head(4).tail(1)

        # Merge NSForest results with manual author cell set to CL
        # term mapping since author cell sets may not align exactly
        author_to_cl_results = author_to_cl_results.merge(
            nsforest_results[
                [
                    "clusterName",
                    "clusterSize",
                    "NSForest_markers",
                    "binary_genes",
                    "uuid",
                ]
            ].copy(),
            left_on="author_cell_set",
            right_on="clusterName",
        )

        print(f"Creating tuples from {author_to_cl_path}")
        author_to_cl_tuples = create_tuples_from_author_to_cl(
            author_to_cl_results,
            dataset_version_id_list,
            cellxgene_results,
        )
        if summarize:
            output_dirpath = TUPLES_DIRPATH / "summaries"
        else:
            output_dirpath = TUPLES_DIRPATH
        with open(
            output_dirpath / author_to_cl_path.name.replace(".csv", ".json"),
            "w",
        ) as f:
            data = {}
            if summarize:
                data["results"] = author_to_cl_results.to_dict()
            data["tuples"] = author_to_cl_tuples
            json.dump(data, f, indent=4)

        if summarize:
            break


if __name__ == "__main__":
    main(summarize=True)
    main()
