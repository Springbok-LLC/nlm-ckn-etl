"""Create tuples from author-to-CL mapping results using schema entities.

Produces CellType, CellSet, CellSetDataset, and Publication
associations from manual cell type mapping data.
"""

import pandas as pd
from rdflib.term import Literal, URIRef

from LoaderUtilities import (
    DEPRECATED_TERMS,
    MIN_CLUSTER_SIZE,
    PURLBASE,
    RDFSBASE,
    get_cellxgene_harvester_data,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_results_sources,
    hyphenate,
    load_results,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    curie_to_term,
    make_anatomical_structure,
    make_cell_set,
    make_cell_set_dataset,
    make_cell_type,
    make_gene,
    make_publication,
    parse_string_list,
    write_tuples,
)


def create_tuples(
    author_to_cl_results: pd.DataFrame,
    dataset_version_ids: list[str],
    harvester_data: pd.DataFrame | None = None,
) -> list[tuple]:
    """Create tuples from author-to-CL mapping results.

    Produces:
    - CellTypePartOfAnatomicalStructure
    - CellSetComposedPrimarilyOfCellType
    - CellTypeHasExemplarDataCellSetDataset
    - CellSetDatasetHasSourcePublication
    - CellTypeExpressesGene (for each marker + binary gene)
    """
    tuples = []

    for _, row in author_to_cl_results.iterrows():
        uuid = row["uuid"]
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        cell_type = make_cell_type(row)
        anat = make_anatomical_structure(row)
        if cell_type is None or anat is None:
            continue

        cl_term = curie_to_term(cell_type.ontology_purl)
        uberon_term = curie_to_term(anat.ontology_purl)
        if cl_term in DEPRECATED_TERMS:
            print(f"Warning: CL term {cl_term} deprecated")
        if uberon_term in DEPRECATED_TERMS:
            print(f"Warning: UBERON term {uberon_term} deprecated")

        author_cell_set = hyphenate(str(row.get("author_cell_set", "")))
        markers = parse_string_list(str(row.get("NSForest_markers", "[]")))
        binary_genes = parse_string_list(str(row.get("binary_genes", "[]")))

        doi = row.get("DOI")
        collection_id = row.get("collection_id")
        collection_version_id = row.get("collection_version_id")
        dataset_version_id = row.get("dataset_version_id")

        cell_set = make_cell_set(
            row,
            cell_type=cell_type,
            uberon_curie=anat.ontology_purl,
            doi=str(doi) if pd.notna(doi) else None,
            markers=markers,
            binary_genes=binary_genes,
            collection_id=str(collection_id) if pd.notna(collection_id) else None,
            dataset_version_id=(
                str(dataset_version_id) if pd.notna(dataset_version_id) else None
            ),
        )
        ctx = {"uuid": uuid}
        annotated = set()

        # CellType part_of AnatomicalStructure
        assoc = ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
            subject=cell_type, predicate="part_of", object=anat,
        )
        tuples.extend(association_to_tuples(assoc, ctx, source="Manual Mapping", annotated_terms=annotated))

        # CellSet composed_primarily_of CellType
        assoc = ASSOCIATION_CLASSES["CellSetComposedPrimarilyOfCellType"](
            subject=cell_set, predicate="composed_primarily_of", object=cell_type,
        )
        tuples.extend(association_to_tuples(assoc, ctx, source="Manual Mapping", annotated_terms=annotated))

        # Edge annotations on CS→CellType: Match and Mapping_method
        cs_uri = URIRef(f"{PURLBASE}/CS_{author_cell_set}-{uuid}")
        ct_uri = URIRef(f"{PURLBASE}/{cl_term}")
        pred_uri = URIRef(f"{PURLBASE}/RO_0002473")
        match_val = row.get("match")
        if pd.notna(match_val):
            tuples.append(
                (cs_uri, pred_uri, ct_uri,
                 URIRef(f"{RDFSBASE}#Match"), Literal(str(match_val)))
            )
        method_val = row.get("mapping_method")
        if pd.notna(method_val):
            tuples.append(
                (cs_uri, pred_uri, ct_uri,
                 URIRef(f"{RDFSBASE}#Mapping_method"), Literal(str(method_val)))
            )

        # CellType has_exemplar_data CellSetDataset
        for dvid in dataset_version_ids:
            harvester_row = None
            if harvester_data is not None and not harvester_data.empty:
                match_df = harvester_data[
                    harvester_data["dataset_version_id"] == dvid
                ]
                if not match_df.empty:
                    harvester_row = match_df.iloc[0]

            csd = make_cell_set_dataset(
                dvid,
                harvester_row=harvester_row,
                doi=str(doi) if pd.notna(doi) else None,
                collection_id=(
                    str(collection_id) if pd.notna(collection_id) else None
                ),
                collection_version_id=(
                    str(collection_version_id)
                    if pd.notna(collection_version_id)
                    else None
                ),
            )
            assoc = ASSOCIATION_CLASSES["CellTypeHasExemplarDataCellSetDataset"](
                subject=cell_type, predicate="has_exemplar_data", object=csd,
            )
            tuples.extend(
                association_to_tuples(assoc, ctx, source="Manual Mapping", annotated_terms=annotated)
            )

            # CellSetDataset source Publication
            pub = make_publication(row)
            if pub is not None:
                assoc = ASSOCIATION_CLASSES["CellSetDatasetHasSourcePublication"](
                    subject=csd, predicate="source", object=pub,
                )
                tuples.extend(
                    association_to_tuples(assoc, ctx, source="Manual Mapping", annotated_terms=annotated)
                )

        # CellType expresses Gene (for each marker and binary gene)
        for gene_symbol in markers + binary_genes:
            gene = make_gene(gene_symbol)
            assoc = ASSOCIATION_CLASSES["CellTypeExpressesGene"](
                subject=cell_type, predicate="selectively_expresses", object=gene,
            )
            tuples.extend(
                association_to_tuples(assoc, ctx, source="Manual Mapping", annotated_terms=annotated)
            )

    return tuples


def main():
    """Run Mapping tuple writer for all datasets."""
    results_sources = get_results_sources()
    harvester_data = get_cellxgene_harvester_data(results_sources)
    file_paths = get_dataset_file_paths(results_sources)
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)

    for nsforest_path, mapping_path, scores_path, dvids in zip(
        file_paths["nsforest_paths"],
        file_paths["mapping_paths"],
        file_paths["scores_paths"],
        dataset_version_id_lists,
    ):
        if not mapping_path:
            continue

        author_to_cl_path = mapping_path[0]

        nsforest_results = load_results(nsforest_path).sort_values(
            "clusterName", ignore_index=True
        )
        author_to_cl_results = (
            load_results(author_to_cl_path)
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

        print(f"Creating mapping tuples from {author_to_cl_path.name}")
        tuples = create_tuples(author_to_cl_results, dvids, harvester_data)
        if tuples:
            output_name = author_to_cl_path.name.replace(".csv", "-mapping.json")
            write_tuples(tuples, TUPLES_DIRPATH / output_name)


if __name__ == "__main__":
    main()
