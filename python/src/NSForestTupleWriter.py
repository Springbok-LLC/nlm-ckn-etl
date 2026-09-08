"""Create tuples from NSForest results using schema entities.

Produces AnatomicalStructure, BinaryGeneSet, BiomarkerCombination, CellSet,
CellSetDataset, and Gene associations from NSForest results and silhouette
scores.
"""

import pandas as pd
from rdflib.term import Literal, URIRef

import ProductionDataSpecification as spec

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
    get_current_run,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_gene_ensembl_id_to_names_map,
    get_harvester_row,
    get_uberon_root_map,
    hyphenate,
    load_results,
    resolve_summary_root_uberon_term,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    as_float,
    as_int,
    as_str,
    association_to_tuples,
    build_cell_set_dataset,
    cell_set_dataset_name_tuples,
    get_predicate_uri,
    get_tuples_dir,
    parse_string_list,
    remove_protocols,
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


def sampled_tissue_annotation(
    subject_term: str,
    association,
    object_term: str,
    sampled_tissue: str,
) -> list[tuple]:
    """Annotate an anatomical structure edge with the tissue terms sampled.

    Cell sets and cell set datasets connect to the root term of the organ
    they were sampled from, so the descendant terms the dataset summary
    lists are carried on the edge rather than lost.
    """
    if not sampled_tissue:
        return []
    return [
        (
            URIRef(f"{PURLBASE}/{subject_term}"),
            get_predicate_uri(association),
            URIRef(f"{PURLBASE}/{object_term}"),
            URIRef(f"{RDFSBASE}#Sampled_tissue"),
            Literal(sampled_tissue),
        )
    ]


def mean_binary_scores(
    nsforest_results: pd.DataFrame, binary_scores: pd.DataFrame
) -> pd.Series:
    """Return each cluster's mean binary score over its binary gene set.

    The binary scores are published as a gene × cluster matrix alongside
    the results file, so a cluster's score for one of its binary genes is
    the cell at that gene's row and that cluster's column, and the mean
    over the cluster's ``binary_genes`` is the value the schema's
    ``mean_binary_score`` slot wants.

    Genes are matched on the unversioned Ensembl id, as
    :func:`resolve_gene_names` reads them, so a version suffix on either
    side does not silently drop a gene from the mean.  A cluster the
    matrix does not cover, or one none of whose binary genes it lists,
    yields ``None`` rather than a mean over nothing.

    Parameters
    ----------
    nsforest_results : pd.DataFrame
        NSForest results, with ``clusterName`` and ``binary_genes``.
    binary_scores : pd.DataFrame
        Binary scores indexed by gene, one column per cluster.

    Returns
    -------
    pd.Series
        Mean binary score per row of ``nsforest_results``, aligned to its
        index, with ``None`` where no score could be computed.
    """
    means = []
    for _, row in nsforest_results.iterrows():
        cluster = str(row["clusterName"])
        if cluster not in binary_scores.columns:
            means.append(None)
            continue
        by_gene = {
            str(gene).split(".")[0]: score
            for gene, score in binary_scores[cluster].items()
        }
        scores = [
            by_gene[gene]
            for gene in (
                token.split(".")[0]
                for token in parse_string_list(str(row["binary_genes"]))
            )
            if gene in by_gene
        ]
        means.append(sum(scores) / len(scores) if scores else None)
    return pd.Series(means, index=nsforest_results.index, dtype="float64")


def create_tuples(
    nsforest_results: pd.DataFrame,
    summary_data: pd.DataFrame,
    dataset_version_ids: list[str],
    harvester_data: pd.DataFrame | None = None,
    cluster_dvid_map: dict[str, str] | None = None,
    root_uberon_term: str | None = None,
) -> list[tuple]:
    """Create tuples from NSForest results.

    Produces:
    - CellSetDatasetIsAboutAnatomicalStructure (dataset-scope, per UBERON term)
    - CellSetDatasetIsAboutCellSet
    - CellSetDerivesFromAnatomicalStructure
    - CellSetHasBinaryGeneSetBinaryGeneSet
    - CellSetHasCharacterizingMarkerSetBiomarkerCombination
    - CellSetSelectivelyExpressesGene (for each binary gene)
    - GenePartOfBinaryGeneSet (for each binary gene)
    - GenePartOfBiomarkerCombination (for each marker gene)

    Parameters
    ----------
    nsforest_results : pd.DataFrame
        DataFrame containing NSForest results with columns: clusterName,
        clusterSize, f_score, precision, NSForest_markers, binary_genes,
        uuid. May include a 'median' column from merged silhouette scores,
        and a 'mean_binary_score' column from merged binary scores.
    summary_data : pd.DataFrame
        DataFrame containing dataset summary with a tissue_ontology_term_id
        column.
    dataset_version_ids : list[str]
        List of dataset version identifiers for CellSetDataset creation.
    harvester_data : pd.DataFrame, optional
        DataFrame containing CELLxGENE harvester metadata for enriching
        CellSetDataset entities.
    cluster_dvid_map : dict[str, str], optional
        Mapping from ``clusterName`` to the single ``dataset_version_id`` the
        cluster belongs to, used only when ``dataset_version_ids`` spans more
        than one dataset (in prod, only Jorstad).  When provided, each cell
        set emits a single ``is_about`` edge from its resolved dataset instead
        of fanning out to every dataset.  A cluster the map cannot resolve to
        a known dataset raises (the multi-dataset mapping is incomplete).
        ``None`` for single-dataset results files, where the lone dataset is
        the only possible member.
    root_uberon_term : str, optional
        The root UBERON CURIE of the organ the dataset was sampled from, as
        resolved by ``LoaderUtilities.resolve_summary_root_uberon_term``.  Cell
        sets and the dataset connect to it alone, and the summary's descendant
        tissue terms become an edge annotation.  ``None`` for an organ with no
        root term, where the descendant terms are connected as before.

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
        sampled_terms = []
    else:
        sampled_terms = [
            t.replace(":", "_").strip()
            for t in str(summary_data.iloc[0]["tissue_ontology_term_id"]).split("|")
        ]

    # A dataset is sampled from one organ, so its cell sets connect to that
    # organ's root term rather than to each descendant term the summary lists.
    # The sampled terms survive as an edge annotation.  A dataset whose organ
    # has no root term keeps its descendant terms.
    if root_uberon_term:
        uberon_terms = [root_uberon_term.replace(":", "_")]
    else:
        uberon_terms = sampled_terms
    sampled_tissue = "|".join(t.replace("_", ":") for t in sampled_terms)

    # Build CellSetDataset entities once per dvid (reused across clusters).
    csd_by_dvid: dict[str, tuple] = {}
    for dvid in dataset_version_ids:
        # Take this dataset's own summary row.  A multi-dataset summary
        # (Jorstad) has one row per dataset, and build_cell_set_dataset reads
        # the first, so passing the whole frame would give every dataset the
        # first one's title and statistics.  A dvid the summary does not
        # cover keeps the empty frame, which build_cell_set_dataset reads as
        # "no summary" rather than as the first dataset's.
        summary_row = summary_data
        if (
            summary_data is not None
            and not summary_data.empty
            and "dataset_version_id" in summary_data.columns
        ):
            summary_row = summary_data[
                summary_data["dataset_version_id"].astype(str) == str(dvid)
            ]
        organ = (
            summary_row.iloc[0].get("organ")
            if summary_row is not None and not summary_row.empty
            else None
        )
        harvester_row = get_harvester_row(harvester_data, dvid, organ)
        csd_by_dvid[dvid] = build_cell_set_dataset(dvid, summary_row, harvester_row)

    # CellSetDataset is_about AnatomicalStructure (dataset-scope)
    for dvid, (csd, citation) in csd_by_dvid.items():
        csd_term = f"CSD_{csd.dataset_identifier}"
        for uberon_term in uberon_terms:
            anat = AnatomicalStructure(ontology_purl=uberon_term.replace("_", ":"))
            assoc = ASSOCIATION_CLASSES["CellSetDatasetIsAboutAnatomicalStructure"](
                subject=csd,
                predicate="nlm-ckn:is_about",
                object=anat,
            )
            tuples.extend(association_to_tuples(assoc, source="CELLxGENE"))
            tuples.extend(
                sampled_tissue_annotation(
                    csd_term, assoc, uberon_term, sampled_tissue
                )
            )
        tuples.extend(
            cell_set_dataset_name_tuples(csd_term, citation, csd.dataset_name)
        )

    for _, row in nsforest_results.iterrows():
        uuid = row["uuid"]
        cluster_name = hyphenate(row["clusterName"])
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        marker_genes = resolve_gene_names(
            parse_string_list(str(row["NSForest_markers"])), ensembl_id_to_names
        )
        binary_genes = resolve_gene_names(
            parse_string_list(str(row["binary_genes"])), ensembl_id_to_names
        )

        bmc = BiomarkerCombination(
            markers=",".join(marker_genes),
            f_beta_score=float(row["f_score"]) if pd.notna(row["f_score"]) else None,
        )
        bgs = BinaryGeneSet(
            markers=",".join(binary_genes),
            mean_binary_score=as_float(row, "mean_binary_score"),
        )

        # Resolve the cluster's dataset before building the cell set, so the
        # cell set can carry that dataset's metadata.  A cell set is described
        # by exactly one dataset.  For a single-dataset results file that is
        # the lone dvid; for a multi-dataset file (Jorstad) the cluster's
        # dataset comes from cluster_dvid_map.  Fanning out to every dvid
        # would falsely assert the cell set is described by datasets it was
        # not taken from.
        if cluster_dvid_map is not None:
            describing_dvid = cluster_dvid_map.get(str(row["clusterName"]))
            if describing_dvid is None or describing_dvid not in csd_by_dvid:
                raise Exception(
                    f"No dataset_version_id resolved for cluster "
                    f"{row['clusterName']!r} in a multi-dataset results file; "
                    "cluster_cid_mapping is incomplete"
                )
            describing_dvids = [describing_dvid]
        else:
            if len(csd_by_dvid) > 1:
                raise Exception(
                    "Multi-dataset results require cluster_dvid_map to resolve "
                    "per-cluster is_about edges"
                )
            describing_dvids = list(csd_by_dvid.keys())
        describing_csd = csd_by_dvid[describing_dvids[0]][0]

        # A dataset is tissue-filtered before clustering, so the dataset's
        # organ is the cell set's organ, matching the derives_from edge below.
        cell_set_anatomical_structure = (
            uberon_terms[0].replace("_", ":") if uberon_terms else None
        )

        cell_set = CellSet(
            author_cell_term=cluster_name,
            # Dataset-scoped metadata, carried onto the cell set so that every
            # cell set is described the same way whether or not it also has a
            # manual cell type mapping (Springbok-LLC/nlm-ckn-etl#63).
            species=describing_csd.species,
            assay=describing_csd.assay,
            donor_age=describing_csd.donor_age,
            anatomical_structure=cell_set_anatomical_structure,
            publication=describing_csd.publication,
            dataset_name=describing_csd.dataset_name,
            cellxgene_collection=remove_protocols(
                describing_csd.cellxgene_collection
            ),
            # Built the way MappingTupleWriter builds it, so a cell set names
            # its dataset identically whether or not it is also mapped.
            cellxgene_dataset=(
                f"datasets.cellxgene.cziscience.com/{describing_dvids[0]}.h5ad"
            ),
            cluster_annotation=(
                as_str(row, "cluster_header") or describing_csd.cluster_annotation
            ),
            cell_count=as_int(row, "clusterSize"),
            cluster_cell_count=as_int(row, "clusterSize"),
            biomarker_combination=",".join(marker_genes),
            binary_gene_set=",".join(binary_genes),
            expressed_genes=",".join(binary_genes),
            silhouette_score=as_float(row, "median"),
            median_silhouette=as_float(row, "median"),
            mean_silhouette=as_float(row, "mean"),
            standard_deviation_of_silhouette=as_float(row, "std"),
            first_quartile_silhouette=as_float(row, "q1"),
            third_quartile_silhouette=as_float(row, "q3"),
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
                predicate="nlm-ckn:derives_from",
                object=anat,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="CELLxGENE", annotated_terms=annotated
                )
            )
            tuples.extend(
                sampled_tissue_annotation(
                    f"CS_{uuid}", assoc, uberon_term, sampled_tissue
                )
            )

        # CellSet has_binary_gene_set BinaryGeneSet
        assoc = ASSOCIATION_CLASSES["CellSetHasBinaryGeneSetBinaryGeneSet"](
            subject=cell_set,
            predicate="nlm-ckn:has_binary_gene_set",
            object=bgs,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="NS-Forest", annotated_terms=annotated
            )
        )

        # CellSet selectively_expresses Gene, and Gene part_of BinaryGeneSet
        # (for each binary gene).  The cell set has the binary gene set as a
        # whole (edge above), and selectively expresses each of its binary
        # genes.  The binary genes always include the marker genes, so the
        # marker genes keep their selectively_expresses edges too.
        for gene_symbol in binary_genes:
            gene = Gene(gene_symbol=gene_symbol)
            assoc = ASSOCIATION_CLASSES["CellSetSelectivelyExpressesGene"](
                subject=cell_set,
                predicate="nlm-ckn:selectively_expresses",
                object=gene,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="NS-Forest", annotated_terms=annotated
                )
            )
            assoc = ASSOCIATION_CLASSES["GenePartOfBinaryGeneSet"](
                subject=gene,
                predicate="nlm-ckn:part_of",
                object=bgs,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="NS-Forest", annotated_terms=annotated
                )
            )

        # CellSet has_characterizing_marker_set BiomarkerCombination
        assoc = ASSOCIATION_CLASSES[
            "CellSetHasCharacterizingMarkerSetBiomarkerCombination"
        ](
            subject=cell_set,
            predicate="nlm-ckn:has_characterizing_marker_set",
            object=bmc,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="NS-Forest", annotated_terms=annotated
            )
        )

        # Extra edge annotations from raw data columns on CS→BMC edge
        cs_uri = URIRef(f"{PURLBASE}/CS_{uuid}")
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

        # CellSetDataset is_about CellSet, for the dataset resolved above.
        for dvid in describing_dvids:
            csd, _ = csd_by_dvid[
                dvid
            ]  # (CellSetDataset, citation); citation unused here
            assoc = ASSOCIATION_CLASSES["CellSetDatasetIsAboutCellSet"](
                subject=csd,
                predicate="nlm-ckn:is_about",
                object=cell_set,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="CELLxGENE", annotated_terms=annotated
                )
            )

        # Gene part_of BiomarkerCombination (for each marker gene).  The
        # marker genes are the subset of the binary genes NSForest chose as
        # the cell set's characterizing marker set; their
        # selectively_expresses edges are emitted with the binary genes above.
        for gene_symbol in marker_genes:
            gene = Gene(gene_symbol=gene_symbol)
            assoc = ASSOCIATION_CLASSES["GenePartOfBiomarkerCombination"](
                subject=gene,
                predicate="nlm-ckn:part_of",
                object=bmc,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="NS-Forest", annotated_terms=annotated
                )
            )

    return tuples


def main():
    """Run NSForest tuple writer for all datasets.

    Loads results sources, resolves file paths, and creates tuples
    for each NSForest results file. Writes one JSON tuple file per
    dataset.
    """
    results_dir = get_current_run().results_dir
    harvester_data = get_cellxgene_harvester_data(results_dir)
    uberon_root_map = get_uberon_root_map(results_dir)
    file_paths = get_dataset_file_paths(results_dir)
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)

    for (
        nsforest_path,
        scores_path,
        summary_path,
        mapping_path,
        binary_scores_path,
        dvids,
    ) in zip(
        file_paths["nsforest_paths"],
        file_paths["scores_paths"],
        file_paths["summary_paths"],
        file_paths["mapping_paths"],
        file_paths["binary_scores_paths"],
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

        # After the silhouette merge, so the column aligns to the merged
        # frame's index.  The binary scores are sparse in prod, and a results
        # set without them simply leaves mean_binary_score empty.
        # (pd.read_csv, not load_results: the matrix is gene-indexed and has
        # no cluster rows for load_results to add a uuid column to.)
        if binary_scores_path:
            binary_scores = pd.read_csv(
                binary_scores_path[0],
                index_col=spec.BINARY_SCORES_GENE_INDEX_COLUMN,
            )
            nsforest_results["mean_binary_score"] = mean_binary_scores(
                nsforest_results, binary_scores
            )

        summary_data = load_results(summary_path[0]) if summary_path else pd.DataFrame()

        # When a results file spans more than one dataset (in prod, only
        # Jorstad), each cell set belongs to exactly one of them; the
        # per-cluster dataset_version_id lives in the reference
        # cluster_cid_mapping.  Build the clusterName -> dataset_version_id map
        # so create_tuples emits a single is_about edge per cell set.  This is
        # an enforced invariant: a multi-dataset file must carry a mapping with
        # a dataset_version_id column, else we cannot resolve which dataset
        # describes a cell set.  Single-dataset files need no mapping (the lone
        # dvid is the only dataset that can describe it).
        # (pd.read_csv, not load_results: avoid load_results rewriting the
        # mapping CSV with a uuid column we do not use here.)
        cluster_dvid_map = None
        if len(set(dvids)) > 1:
            if not mapping_path:
                raise Exception(
                    f"{nsforest_path.name} spans {len(set(dvids))} datasets but "
                    "has no cluster_cid_mapping to resolve per-cluster membership"
                )
            mapping_df = pd.read_csv(mapping_path[0])
            if "dataset_version_id" not in mapping_df.columns:
                raise Exception(
                    f"{mapping_path[0].name} has no dataset_version_id column to "
                    f"resolve per-cluster membership for {nsforest_path.name}"
                )
            if "cluster_name" not in mapping_df.columns:
                raise Exception(
                    f"{mapping_path[0].name} has no cluster_name column to "
                    f"resolve per-cluster membership for {nsforest_path.name}"
                )
            cluster_dvid_map = dict(
                zip(
                    mapping_df["cluster_name"].astype(str),
                    mapping_df["dataset_version_id"].astype(str),
                )
            )

        root_uberon_term = resolve_summary_root_uberon_term(
            summary_data, uberon_root_map
        )

        print(f"Creating NSForest tuples from {nsforest_path.name}")
        tuples = create_tuples(
            nsforest_results,
            summary_data,
            dvids,
            harvester_data,
            cluster_dvid_map,
            root_uberon_term,
        )
        if tuples:
            output_name = nsforest_path.name.replace(".csv", "-nsforest.json")
            write_tuples(tuples, get_tuples_dir() / output_name)


if __name__ == "__main__":
    main()
