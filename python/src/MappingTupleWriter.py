"""Create tuples from author-to-CL mapping results using schema entities.

Produces AnatomicalStructure, CellSet, CellSetDataset, and CellType
associations from manual cell type mapping data.
"""

import re

import pandas as pd
from rdflib.term import Literal, URIRef

from LoaderUtilities import (
    DEPRECATED_TERMS,
    MIN_CLUSTER_SIZE,
    PURLBASE,
    RDFSBASE,
    get_cellxgene_harvester_data,
    get_current_run,
    get_dataset_file_paths,
    get_gene_ensembl_id_to_names_map,
    get_uberon_root_map,
    hyphenate,
    load_results,
    resolve_summary_root_uberon_term,
)

from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    CellSet,
    CellType,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    association_to_tuples,
    build_cell_set_dataset,
    cell_set_dataset_name_tuples,
    curie_to_term,
    get_tuples_dir,
    parse_string_list,
    purl_to_curie,
    resolve_gene_names,
    write_tuples,
)


def create_tuples(
    mapping_results: pd.DataFrame,
    summary_data: pd.DataFrame,
    harvester_data: pd.DataFrame | None = None,
    uberon_root_map: dict | None = None,
) -> list[tuple]:
    """Create tuples from cluster_cid author-to-CL mapping results.

    Produces:
    - CellSetComposedPrimarilyOfCellType
    - CellTypeHasExemplarDataCellSetDataset

    Parameters
    ----------
    mapping_results : pd.DataFrame
        Merged cluster_cid_mapping + NSForest results, with columns from the
        mapping (``dataset_version_id``, ``cluster_name``, ``skos``,
        ``manual_mapped_cid``, ``cell_ontology_id``) and from NSForest
        (``clusterName``, ``clusterSize``, ``NSForest_markers``,
        ``binary_genes``, ``uuid``).
    summary_data : pd.DataFrame
        master_dataset_summary for this results set (one row per
        ``dataset_version_id``), supplying tissue and dataset metadata.
    harvester_data : pd.DataFrame, optional
        CELLxGENE harvester metadata, keyed by ``dataset_version_id``.
    uberon_root_map : dict, optional
        Organ to UBERON root mapping from ``LoaderUtilities.get_uberon_root_map``,
        used to set each cell set's anatomical structure to the root term of the
        organ its dataset was sampled from.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []
    ensembl_id_to_names = get_gene_ensembl_id_to_names_map()

    for _, row in mapping_results.iterrows():
        uuid = row["uuid"]
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        # Build CellType.  The manual curation (manual_mapped_cid) wins over the
        # automatic mapping (cell_ontology_id): manual is the more-specific,
        # human-corrected term, with cell_ontology_id the fallback when no manual
        # curation exists.
        cl_id_raw = row.get("manual_mapped_cid")
        if pd.isna(cl_id_raw) or str(cl_id_raw).strip() == "":
            cl_id_raw = row.get("cell_ontology_id")
        if pd.isna(cl_id_raw) or str(cl_id_raw).strip() == "":
            print(
                f"Warning: No cell ontology ID for cluster "
                f"{row.get('cluster_name')!r}"
            )
            continue
        cl_curie = purl_to_curie(str(cl_id_raw))
        if not re.match(r"CL:\d{7}$", cl_curie):
            print(f"Warning: CL CURIE unexpected: {cl_curie}")
            continue
        cell_type = CellType(ontology_purl=cl_curie, label=None)
        cl_term = curie_to_term(cell_type.ontology_purl)
        if cl_term in DEPRECATED_TERMS:
            print(f"Warning: CL term {cl_term} deprecated")

        # Resolve this cluster's dataset (per-row dataset_version_id: Jorstad
        # maps each cluster to its own dataset) and its summary row.
        dataset_version_id = row.get("dataset_version_id")
        summary_row = pd.DataFrame()
        if not summary_data.empty and "dataset_version_id" in summary_data.columns:
            summary_row = summary_data[
                summary_data["dataset_version_id"].astype(str)
                == str(dataset_version_id)
            ]

        # Tissue (UBERON) comes from the dataset summary.  Datasets are
        # tissue-filtered during processing, so the dataset's tissue is the
        # cluster's tissue: the root term of the organ the dataset was sampled
        # from, matching the CellSet -> AnatomicalStructure edge
        # NSForestTupleWriter emits.  Fall back to the first of the summary's
        # tissue terms for an organ with no root term.
        anat = None
        root_term = resolve_summary_root_uberon_term(summary_row, uberon_root_map or {})
        if root_term is None and not summary_row.empty:
            raw_tissue = summary_row.iloc[0].get("tissue_ontology_term_id")
            if pd.notna(raw_tissue):
                terms = [
                    t.strip().replace("_", ":")
                    for t in str(raw_tissue).split("|")
                    if t.strip()
                ]
                root_term = terms[0] if terms else None
        if root_term is not None:
            anat = AnatomicalStructure(ontology_purl=root_term)
            uberon_term = curie_to_term(anat.ontology_purl)
            if uberon_term in DEPRECATED_TERMS:
                print(f"Warning: UBERON term {uberon_term} deprecated")

        author_cell_set = hyphenate(str(row.get("cluster_name", "")))
        markers = resolve_gene_names(
            parse_string_list(str(row.get("NSForest_markers", "[]"))),
            ensembl_id_to_names,
        )
        binary_genes = resolve_gene_names(
            parse_string_list(str(row.get("binary_genes", "[]"))),
            ensembl_id_to_names,
        )

        doi = summary_row.iloc[0].get("doi") if not summary_row.empty else None

        cell_set = CellSet(
            author_cell_term=author_cell_set,
            ontology_purl=cell_type.ontology_purl,
            anatomical_structure=anat.ontology_purl if anat is not None else None,
            species="Homo sapiens",
            publication=str(doi) if pd.notna(doi) else None,
            cell_count=int(cluster_size) if pd.notna(cluster_size) else None,
            biomarker_combination=",".join(markers) if markers else None,
            binary_gene_set=",".join(binary_genes) if binary_genes else None,
            expressed_genes=",".join(binary_genes) if binary_genes else None,
            cellxgene_dataset=(
                f"datasets.cellxgene.cziscience.com/{dataset_version_id}.h5ad"
                if pd.notna(dataset_version_id)
                else None
            ),
        )
        ctx = {"uuid": uuid}
        annotated = set()

        # CellSet composed_primarily_of CellType
        assoc = ASSOCIATION_CLASSES["CellSetComposedPrimarilyOfCellType"](
            subject=cell_set,
            predicate="nlm-ckn:composed_primarily_of",
            object=cell_type,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="Manual Mapping", annotated_terms=annotated
            )
        )

        # Edge annotation on CS→CellType: the SKOS mapping relation
        # (replaces the old Match / Mapping_method annotations).
        cs_uri = URIRef(f"{PURLBASE}/CS_{uuid}")
        ct_uri = URIRef(f"{PURLBASE}/{cl_term}")
        pred_uri = URIRef(f"{PURLBASE}/RO_0002473")
        skos_val = row.get("skos")
        if pd.notna(skos_val):
            tuples.append(
                (
                    cs_uri,
                    pred_uri,
                    ct_uri,
                    URIRef(f"{RDFSBASE}#skos"),
                    Literal(str(skos_val)),
                )
            )

        # CellType has_exemplar_data CellSetDataset (this cluster's single
        # dataset).  Collection / cell-count metadata come from the CELLxGENE
        # harvester row, dataset metadata from the matching summary row.
        harvester_row = None
        if harvester_data is not None and not harvester_data.empty:
            match_df = harvester_data[
                harvester_data["dataset_version_id"] == dataset_version_id
            ]
            if not match_df.empty:
                harvester_row = match_df.iloc[0]

        csd, citation = build_cell_set_dataset(
            dataset_version_id,
            summary_data=summary_row if not summary_row.empty else None,
            harvester_row=harvester_row,
            doi=str(doi) if pd.notna(doi) else None,
        )
        assoc = ASSOCIATION_CLASSES["CellTypeHasExemplarDataCellSetDataset"](
            subject=cell_type,
            predicate="nlm-ckn:has_exemplar_data",
            object=csd,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="Manual Mapping", annotated_terms=annotated
            )
        )
        tuples.extend(
            cell_set_dataset_name_tuples(
                f"CSD_{csd.dataset_identifier}", citation, csd.dataset_name
            )
        )

    return tuples


def main():
    """Run Mapping tuple writer for all datasets.

    Loads results sources, resolves file paths, merges NSForest
    results with author-to-CL mapping data, and creates tuples for
    each dataset with a mapping file. Writes one JSON tuple file per
    dataset.
    """
    results_dir = get_current_run().results_dir
    harvester_data = get_cellxgene_harvester_data(results_dir)
    uberon_root_map = get_uberon_root_map(results_dir)
    file_paths = get_dataset_file_paths(results_dir)

    for nsforest_path, mapping_path, summary_path in zip(
        file_paths["nsforest_paths"],
        file_paths["mapping_paths"],
        file_paths["summary_paths"],
    ):
        if not mapping_path:
            continue

        cluster_cid_path = mapping_path[0]

        nsforest_results = load_results(nsforest_path).sort_values(
            "clusterName", ignore_index=True
        )
        mapping_results = load_results(cluster_cid_path).sort_values(
            "cluster_name", ignore_index=True
        )
        # load_results adds a uuid column to every CSV; drop the mapping's so the
        # merged uuid (the CellSet identity) comes from the NSForest cluster row,
        # not a colliding uuid_x/uuid_y pair.
        mapping_results = mapping_results.drop(columns=["uuid"], errors="ignore")
        mapping_results = mapping_results.merge(
            nsforest_results[
                [
                    "clusterName",
                    "clusterSize",
                    "NSForest_markers",
                    "binary_genes",
                    "uuid",
                ]
            ].copy(),
            left_on="cluster_name",
            right_on="clusterName",
        )

        summary_data = load_results(summary_path[0]) if summary_path else pd.DataFrame()

        print(f"Creating mapping tuples from {cluster_cid_path.name}")
        tuples = create_tuples(
            mapping_results, summary_data, harvester_data, uberon_root_map
        )
        if tuples:
            output_name = cluster_cid_path.name.replace(".csv", "-mapping.json")
            write_tuples(tuples, get_tuples_dir() / output_name)


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    main()
