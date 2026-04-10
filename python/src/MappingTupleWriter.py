"""Create tuples from author-to-CL mapping results using schema entities.

Produces AnatomicalStructure, CellSet, CellType, Gene, and Publication
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
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_gene_ensembl_id_to_names_map,
    get_results_sources,
    hyphenate,
    load_results,
)

from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    CellSet,
    CellType,
    Gene,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    build_cell_set_dataset,
    curie_to_term,
    parse_string_list,
    purl_to_curie,
    resolve_gene_names,
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
    - CellTypeSelectivelyExpressesGene (for each marker + binary gene)

    Parameters
    ----------
    author_to_cl_results : pd.DataFrame
        DataFrame containing merged author-to-CL mapping and NSForest
        results with columns: cell_ontology_id, cell_ontology_term,
        uberon_entity_id, uberon_entity_term, author_cell_set,
        clusterName, clusterSize, NSForest_markers, binary_genes,
        uuid, PMID, PMCID, DOI, match, mapping_method, collection_id,
        collection_version_id, dataset_version_id.
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

    for _, row in author_to_cl_results.iterrows():
        uuid = row["uuid"]
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        # Build CellType from mapping row
        cl_id_raw = row.get("cell_ontology_id", "")
        if not cl_id_raw:
            print(f"Warning: No cell ontology ID for mapping row uuid={uuid}")
            continue
        cl_curie = purl_to_curie(str(cl_id_raw))
        if not re.match(r"CL:\d{7}$", cl_curie):
            print(f"Warning: CL CURIE unexpected: {cl_curie}")
            continue
        cell_type = CellType(
            ontology_purl=cl_curie,
            label=row.get("cell_ontology_term"),
        )

        # Build AnatomicalStructure from mapping row
        uberon_raw = row.get("uberon_entity_id", "")
        if not uberon_raw:
            print("Warning: No UBERON entity id")
            continue
        uberon_curie = purl_to_curie(str(uberon_raw))
        anat = AnatomicalStructure(
            ontology_purl=uberon_curie,
            label=row.get("uberon_entity_term"),
        )

        cl_term = curie_to_term(cell_type.ontology_purl)
        uberon_term = curie_to_term(anat.ontology_purl)
        if cl_term in DEPRECATED_TERMS:
            print(f"Warning: CL term {cl_term} deprecated")
        if uberon_term in DEPRECATED_TERMS:
            print(f"Warning: UBERON term {uberon_term} deprecated")

        author_cell_set = hyphenate(str(row.get("author_cell_set", "")))
        markers = resolve_gene_names(
            parse_string_list(str(row.get("NSForest_markers", "[]"))),
            ensembl_id_to_names,
        )
        binary_genes = resolve_gene_names(
            parse_string_list(str(row.get("binary_genes", "[]"))),
            ensembl_id_to_names,
        )

        doi = row.get("DOI")
        collection_id = row.get("collection_id")
        collection_version_id = row.get("collection_version_id")
        dataset_version_id = row.get("dataset_version_id")

        cell_set = CellSet(
            author_cell_term=author_cell_set,
            ontology_purl=cell_type.ontology_purl,
            anatomical_structure=anat.ontology_purl,
            species="Homo sapiens",
            publication=str(doi) if pd.notna(doi) else None,
            cell_count=int(cluster_size) if pd.notna(cluster_size) else None,
            biomarker_combination=",".join(markers) if markers else None,
            binary_gene_set=",".join(binary_genes) if binary_genes else None,
            expressed_genes=",".join(binary_genes) if binary_genes else None,
            cellxgene_collection=(
                f"cellxgene.cziscience.com/collections/{collection_id}"
                if pd.notna(collection_id)
                else None
            ),
            cellxgene_dataset=(
                f"datasets.cellxgene.cziscience.com/{dataset_version_id}.h5ad"
                if pd.notna(dataset_version_id)
                else None
            ),
        )
        ctx = {"uuid": uuid}
        annotated = set()

        # CellType part_of AnatomicalStructure
        assoc = ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
            subject=cell_type,
            predicate="part_of",
            object=anat,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="Manual Mapping", annotated_terms=annotated
            )
        )

        # CellSet composed_primarily_of CellType
        assoc = ASSOCIATION_CLASSES["CellSetComposedPrimarilyOfCellType"](
            subject=cell_set,
            predicate="composed_primarily_of",
            object=cell_type,
        )
        tuples.extend(
            association_to_tuples(
                assoc, ctx, source="Manual Mapping", annotated_terms=annotated
            )
        )

        # Edge annotations on CS→CellType: Match and Mapping_method
        cs_uri = URIRef(f"{PURLBASE}/CS_{author_cell_set}-{uuid}")
        ct_uri = URIRef(f"{PURLBASE}/{cl_term}")
        pred_uri = URIRef(f"{PURLBASE}/RO_0002473")
        match_val = row.get("match")
        if pd.notna(match_val):
            tuples.append(
                (
                    cs_uri,
                    pred_uri,
                    ct_uri,
                    URIRef(f"{RDFSBASE}#Match"),
                    Literal(str(match_val)),
                )
            )
        method_val = row.get("mapping_method")
        if pd.notna(method_val):
            tuples.append(
                (
                    cs_uri,
                    pred_uri,
                    ct_uri,
                    URIRef(f"{RDFSBASE}#Mapping_method"),
                    Literal(str(method_val)),
                )
            )

        # CellType has_exemplar_data CellSetDataset
        for dvid in dataset_version_ids:
            harvester_row = None
            if harvester_data is not None and not harvester_data.empty:
                match_df = harvester_data[harvester_data["dataset_version_id"] == dvid]
                if not match_df.empty:
                    harvester_row = match_df.iloc[0]

            csd, citation = build_cell_set_dataset(
                dvid,
                harvester_row=harvester_row,
                doi=str(doi) if pd.notna(doi) else None,
                collection_id=(str(collection_id) if pd.notna(collection_id) else None),
                collection_version_id=(
                    str(collection_version_id)
                    if pd.notna(collection_version_id)
                    else None
                ),
            )
            assoc = ASSOCIATION_CLASSES["CellTypeHasExemplarDataCellSetDataset"](
                subject=cell_type,
                predicate="has_exemplar_data",
                object=csd,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="Manual Mapping", annotated_terms=annotated
                )
            )
            if citation:
                csd_term = f"CSD_{dvid}"
                tuples.append(
                    (
                        URIRef(f"{PURLBASE}/{csd_term}"),
                        URIRef(f"{RDFSBASE}#Citation"),
                        Literal(citation),
                    )
                )

        # CellType expresses Gene (for each marker and binary gene)
        for gene_symbol in markers + binary_genes:
            gene = Gene(gene_symbol=gene_symbol)
            assoc = ASSOCIATION_CLASSES["CellTypeSelectivelyExpressesGene"](
                subject=cell_type,
                predicate="selectively_expresses",
                object=gene,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx, source="Manual Mapping", annotated_terms=annotated
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

        print(f"Creating mapping tuples from {author_to_cl_path.name}")
        tuples = create_tuples(author_to_cl_results, dvids, harvester_data)
        if tuples:
            output_name = author_to_cl_path.name.replace(".csv", "-mapping.json")
            write_tuples(tuples, TUPLES_DIRPATH / output_name)


if __name__ == "__main__":
    main()
