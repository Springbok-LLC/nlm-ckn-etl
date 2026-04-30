"""Create tuples from NSForest results using schema entities.

Produces AnatomicalStructure, BinaryGeneSet, BiomarkerCombination, CellSet,
CellSetDataset, and Gene associations from NSForest results and silhouette
scores.
"""

import pandas as pd
from rdflib.term import Literal, URIRef

from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    BinaryGeneSet,
    BiomarkerCombination,
    CellSet,
    Gene,
)

from LoaderUtilities import (
    MIN_CLUSTER_SIZE,
    PURLBASE,
    RDFSBASE,
    get_cellxgene_harvester_data,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_gene_ensembl_id_to_names_map,
    get_results_sources,
    hyphenate,
    load_results,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    as_float,
    as_int,
    as_str,
    association_to_tuples,
    build_cell_set_dataset,
    get_tuples_dir,
    parse_string_list,
    resolve_gene_names,
    write_tuples,
)

# Extra edge annotations from raw data columns (not on the Pydantic
# entity model) for the CS→BMC edge.
CS_BMC_EDGE_COLUMNS = [
    ("precision", "Precision"),
    ("TN", "TN"),
    ("TP", "TP"),
    ("FN", "FN"),
    ("FP", "FP"),
    ("marker_count", "Marker_count"),
]


def create_tuples(
    nsforest_results: pd.DataFrame,
    summary_data: pd.DataFrame,
    dataset_version_ids: list[str],
    harvester_data: pd.DataFrame | None = None,
) -> list[tuple]:
    """Create tuples from NSForest results.

    Produces:
    - CellSetDatasetIsAboutAnatomicalStructure (dataset-scope, per UBERON term)
    - CellSetDerivesFromAnatomicalStructure
    - CellSetExpressesBinaryGeneSet
    - CellSetHasCharacterizingMarkerSetBiomarkerCombination
    - CellSetMemberOfCellSetDataset
    - GenePartOfBiomarkerCombination (for each marker)
    - BiomarkerCombinationSubclusterOfBinaryGeneSet

    Parameters
    ----------
    nsforest_results : pd.DataFrame
        DataFrame containing NSForest results with columns: clusterName,
        clusterSize, f_score, precision, NSForest_markers, binary_genes,
        uuid. May include a 'median' column from merged silhouette scores.
    summary_data : pd.DataFrame
        DataFrame containing dataset summary with a tissue_ontology_term_id
        column.
    dataset_version_ids : list[str]
        List of dataset version identifiers for CellSetDataset creation.
    harvester_data : pd.DataFrame, optional
        DataFrame containing CELLxGENE harvester metadata for enriching
        CellSetDataset entities.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []
    ensembl_id_to_names = get_gene_ensembl_id_to_names_map()

    # Skip the dataset entirely if no cluster passes the size filter.
    if not (nsforest_results["clusterSize"] >= MIN_CLUSTER_SIZE).any():
        return tuples

    if summary_data.empty:
        uberon_terms = []
    else:
        uberon_terms = [
            t.replace(":", "_").strip()
            for t in str(summary_data.iloc[0]["tissue_ontology_term_id"]).split("|")
        ]

    # Build CellSetDataset entities once per dvid (reused across clusters).
    csd_by_dvid: dict[str, tuple] = {}
    for dvid in dataset_version_ids:
        harvester_row = None
        if harvester_data is not None and not harvester_data.empty:
            match = harvester_data[harvester_data["dataset_version_id"] == dvid]
            if not match.empty:
                harvester_row = match.iloc[0]
        csd_by_dvid[dvid] = build_cell_set_dataset(dvid, summary_data, harvester_row)

    # CellSetDataset is_about AnatomicalStructure (dataset-scope)
    for dvid, (csd, citation) in csd_by_dvid.items():
        for uberon_term in uberon_terms:
            anat = AnatomicalStructure(ontology_purl=uberon_term.replace("_", ":"))
            assoc = ASSOCIATION_CLASSES["CellSetDatasetIsAboutAnatomicalStructure"](
                subject=csd,
                predicate="is_about",
                object=anat,
            )
            tuples.extend(association_to_tuples(assoc, source="NSForest"))
        if citation:
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/CSD_{dvid}"),
                    URIRef(f"{RDFSBASE}#Citation"),
                    Literal(citation),
                )
            )

    for _, row in nsforest_results.iterrows():
        uuid = row["uuid"]
        cluster_name = hyphenate(row["clusterName"])
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        markers = resolve_gene_names(
            parse_string_list(str(row["NSForest_markers"])), ensembl_id_to_names
        )
        binary_genes = resolve_gene_names(
            parse_string_list(str(row["binary_genes"])), ensembl_id_to_names
        )

        bmc = BiomarkerCombination(
            markers=",".join(markers),
            f_beta_score=float(row["f_score"]) if pd.notna(row["f_score"]) else None,
        )
        bgs = BinaryGeneSet(markers=",".join(binary_genes))
        cell_set = CellSet(
            author_cell_term=cluster_name,
            cell_count=as_int(row, "clusterSize"),
            cluster_cell_count=as_int(row, "clusterSize"),
            biomarker_combination=",".join(markers),
            binary_gene_set=",".join(binary_genes),
            expressed_genes=",".join(binary_genes),
            silhouette_score=as_float(row, "median"),
            median_silhouette=as_float(row, "median"),
            mean_silhouette=as_float(row, "mean"),
            standard_deviation_of_silhouette=as_float(row, "std"),
            first_quartile_silhouette=as_float(row, "q1"),
            third_quartile_silhouette=as_float(row, "q3"),
            f_beta_score=as_float(row, "f_score"),
            precision=as_float(row, "precision"),
            recall=as_float(row, "recall"),
            true_positive=as_int(row, "TP"),
            false_positive=as_int(row, "FP"),
            false_negative=as_int(row, "FN"),
            on_target=as_float(row, "onTarget"),
        )
        ctx = {"uuid": uuid}
        annotated = set()

        # CellSet derives_from AnatomicalStructure
        for uberon_term in uberon_terms:
            anat = AnatomicalStructure(ontology_purl=uberon_term.replace("_", ":"))
            assoc = ASSOCIATION_CLASSES["CellSetDerivesFromAnatomicalStructure"](
                subject=cell_set,
                predicate="derives_from",
                object=anat,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="NSForest", annotated_terms=annotated
                )
            )

        # CellSet expresses BinaryGeneSet
        assoc = ASSOCIATION_CLASSES["CellSetExpressesBinaryGeneSet"](
            subject=cell_set,
            predicate="expresses",
            object=bgs,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="NSForest", annotated_terms=annotated
            )
        )

        # CellSet has_characterizing_marker_set BiomarkerCombination
        assoc = ASSOCIATION_CLASSES[
            "CellSetHasCharacterizingMarkerSetBiomarkerCombination"
        ](
            subject=cell_set,
            predicate="has_characterizing_marker_set",
            object=bmc,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="NSForest", annotated_terms=annotated
            )
        )

        # Extra edge annotations from raw data columns on CS→BMC edge
        cs_uri = URIRef(f"{PURLBASE}/CS_{cluster_name}-{uuid}")
        bmc_uri = URIRef(f"{PURLBASE}/BMC_{uuid}")
        pred_uri = URIRef(f"{PURLBASE}/RO_0015004")
        tuples.append(
            (
                cs_uri,
                pred_uri,
                bmc_uri,
                URIRef(f"{RDFSBASE}/#source_algorithm"),
                Literal("NSForest-v4.0_dev"),
            )
        )
        for col, attr in CS_BMC_EDGE_COLUMNS:
            if col in row and pd.notna(row[col]):
                tuples.append(
                    (
                        cs_uri,
                        pred_uri,
                        bmc_uri,
                        URIRef(f"{RDFSBASE}#{attr}"),
                        Literal(str(row[col])),
                    )
                )

        # CellSet member_of CellSetDataset (for each dataset_version_id)
        for dvid, (csd, _) in csd_by_dvid.items():
            assoc = ASSOCIATION_CLASSES["CellSetMemberOfCellSetDataset"](
                subject=cell_set,
                predicate="member_of",
                object=csd,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="NSForest", annotated_terms=annotated
                )
            )

        # Gene part_of BiomarkerCombination (for each marker)
        for gene_symbol in markers:
            gene = Gene(gene_symbol=gene_symbol)
            assoc = ASSOCIATION_CLASSES["GenePartOfBiomarkerCombination"](
                subject=gene,
                predicate="part_of",
                object=bmc,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="NSForest", annotated_terms=annotated
                )
            )

        # BiomarkerCombination subcluster_of BinaryGeneSet
        assoc = ASSOCIATION_CLASSES["BiomarkerCombinationSubclusterOfBinaryGeneSet"](
            subject=bmc,
            predicate="subcluster_of",
            object=bgs,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="NSForest", annotated_terms=annotated
            )
        )

    return tuples


def main():
    """Run NSForest tuple writer for all datasets.

    Loads results sources, resolves file paths, and creates tuples
    for each NSForest results file. Writes one JSON tuple file per
    dataset.
    """
    results_sources = get_results_sources()
    harvester_data = get_cellxgene_harvester_data(results_sources)
    file_paths = get_dataset_file_paths(results_sources)
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)

    for nsforest_path, scores_path, summary_path, dvids in zip(
        file_paths["nsforest_paths"],
        file_paths["scores_paths"],
        file_paths["summary_paths"],
        dataset_version_id_lists,
    ):
        nsforest_results = load_results(nsforest_path).sort_values(
            "clusterName", ignore_index=True
        )

        if scores_path:
            cluster_header = nsforest_results.loc[0, "cluster_header"]
            silhouette_scores = load_results(scores_path[0]).sort_values(
                cluster_header, ignore_index=True
            )
            silhouette_cols = [cluster_header, "median", "mean", "std", "q1", "q3"]
            nsforest_results = nsforest_results.merge(
                silhouette_scores[silhouette_cols].copy(),
                left_on="clusterName",
                right_on=cluster_header,
            )

        summary_data = load_results(summary_path[0]) if summary_path else pd.DataFrame()

        print(f"Creating NSForest tuples from {nsforest_path.name}")
        tuples = create_tuples(nsforest_results, summary_data, dvids, harvester_data)
        if tuples:
            output_name = nsforest_path.name.replace(".csv", "-nsforest.json")
            write_tuples(tuples, get_tuples_dir() / output_name)


if __name__ == "__main__":
    main()
