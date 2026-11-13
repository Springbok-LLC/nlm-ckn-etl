"""Create tuples from CELLxGENE fetched metadata using schema entities.

Produces CellSetDataset described in Publication associations and
annotations from CELLxGENE curation API metadata.
"""

import json

import structlog
from ckn_schema.pydantic.ckn_schema import CellSetDataset, Publication
from rdflib.term import Literal, URIRef

from LoaderUtilities import (
    PURLBASE,
    RDFSBASE,
    get_current_run,
    get_dataset_organs_map,
    get_reference_dataset_version_ids,
)
from logging_setup import configure_logging

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    RejectTally,
    association_to_tuples,
    cell_set_dataset_identifier,
    cell_set_dataset_name_tuples,
    entity_to_term,
    get_tuples_dir,
    normalize_doi,
    remove_protocols,
    require_input,
    write_tuples,
)

_events = structlog.get_logger(__name__)


def create_tuples(
    cellxgene_results: dict,
    dataset_organs: dict | None = None,
    reference_dataset_version_ids: set[str] | None = None,
    rejects: RejectTally | None = None,
) -> list[tuple]:
    """Create tuples from CELLxGENE metadata.

    Produces:
    - CellSetDatasetDescribedInPublication
    - CSD and PUB vertex annotations

    Publications are keyed by DOI, so the datasets of a paper all share
    one publication vertex.

    A source dataset filtered for several organs yields one cell set dataset
    vertex per organ (matching the summary-driven writers), so the publication
    is linked to each filtered dataset.  ``dataset_organs`` supplies the
    source-to-organs map; a source in it with no organ yields a single vertex
    keyed on the source id alone.  When the map is given, a source absent from
    it is skipped: the cached metadata can outlive the dataset (it is reused
    across releases), and no summary mentions it any more.

    Parameters
    ----------
    cellxgene_results : dict
        Dictionary of CELLxGENE metadata keyed by dataset_version_id.
        Each value contains Dataset_name, Organism, Tissue,
        Disease_status, Number_of_cells, Citation, and link fields.
    dataset_organs : dict, optional
        Mapping from ``dataset_version_id`` to the set of organs it was
        filtered for, from ``LoaderUtilities.get_dataset_organs_map``.  When
        omitted or empty, every dataset yields a single source-keyed vertex.
    reference_dataset_version_ids : set[str], optional
        Dataset version ids backed by a cluster_cid_mapping file, as
        returned by ``LoaderUtilities.get_reference_dataset_version_ids``.
        Sets ``CellSetDataset.is_reference_dataset``; ``None`` or absence
        from the set means ``False``, not unset.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []
    rejects = rejects if rejects is not None else RejectTally("cellxgene")
    dataset_organs = dataset_organs or {}
    reference_dataset_version_ids = reference_dataset_version_ids or set()

    # CellSetDataset described_in Publication
    for dataset_version_id, metadata in cellxgene_results.items():
        # Skip cached datasets that no summary mentions (Springbok-LLC/
        # nlm-ckn-etl#171).
        if dataset_organs and dataset_version_id not in dataset_organs:
            rejects.reject("not_in_summary", dataset_version_id)
            continue
        # One vertex per organ the source was filtered for; the source id
        # alone when there is no organ, so the two writers agree on the key.
        organs = sorted(dataset_organs.get(dataset_version_id) or [])
        for organ in organs or [None]:
            csd = CellSetDataset(
                dataset_identifier=cell_set_dataset_identifier(
                    dataset_version_id, organ
                ),
                version=str(dataset_version_id),
                dataset_name=metadata.get("Dataset_name"),
                species=metadata.get("Organism"),
                anatomical_structure=organ or metadata.get("Tissue"),
                disease_status=metadata.get("Disease_status"),
                cell_count=(
                    int(metadata["Number_of_cells"])
                    if metadata.get("Number_of_cells")
                    else None
                ),
                cellxgene_collection=remove_protocols(
                    metadata.get("Link_to_CELLxGENE_collection")
                ),
                cellxgene_dataset=remove_protocols(
                    metadata.get("Link_to_CELLxGENE_dataset")
                ),
                collection_id=metadata.get("Collection_ID"),
                dataset_collection_version=metadata.get("Collection_version_ID"),
                # The DOI the dataset is attributed to, on the dataset vertex
                # as well as the edge.  CELLxGENE knows it for every dataset,
                # so this is the one source that reaches the datasets no
                # dataset summary or harvester row covers
                # (Springbok-LLC/nlm-ckn-etl#63).
                publication=normalize_doi(metadata.get("Link_to_publication")),
                is_reference_dataset=str(dataset_version_id)
                in reference_dataset_version_ids,
            )
            pub = Publication(
                publication_doi=normalize_doi(metadata.get("Link_to_publication")),
                author_list=metadata.get("Author_list"),
                year=str(metadata.get("Year")),
                title=metadata.get("Title"),
                journal=metadata.get("Journal"),
            )
            ctx = {"dataset_version_id": dataset_version_id}

            assoc = ASSOCIATION_CLASSES["CellSetDatasetDescribedInPublication"](
                subject=csd,
                predicate="nlm-ckn:described_in",
                object=pub,
            )
            tuples.extend(association_to_tuples(assoc, ctx, source="CELLxGENE"))

            # Additional annotations not on the PUB and CSD entities
            citation = metadata.get("Citation")
            tuples.extend(
                cell_set_dataset_name_tuples(
                    entity_to_term(csd, ctx), citation, csd.dataset_name
                )
            )
            if citation:
                tuples.append(
                    (
                        URIRef(f"{PURLBASE}/{entity_to_term(pub, ctx)}"),
                        URIRef(f"{RDFSBASE}#Citation"),
                        Literal(citation),
                    )
                )

    return tuples


def main() -> int:
    """Run CELLxGENE tuple writer.

    Loads transformed CELLxGENE metadata and creates tuples for each
    dataset. Writes output to a single JSON tuple file.

    Returns
    -------
    int
        The number of tuples written
    """
    cellxgene_path = get_current_run().external_dir / "cellxgene_transformed.json"
    require_input(cellxgene_path, "CELLxGENE results", "cellxgene_transformed")

    with open(cellxgene_path, "r") as fp:
        cellxgene_results = json.load(fp)
    _events.info(
        "tuple_writer_started",
        source="cellxgene",
        input_path=str(cellxgene_path),
        records_in=len(cellxgene_results),
    )

    dataset_organs = get_dataset_organs_map()
    reference_dataset_version_ids = get_reference_dataset_version_ids()
    rejects = RejectTally("cellxgene")
    try:
        tuples = create_tuples(
            cellxgene_results, dataset_organs, reference_dataset_version_ids, rejects
        )
    finally:
        rejects.log()
    if tuples:
        return write_tuples(tuples, get_tuples_dir() / "cellxgene.json")
    return 0


if __name__ == "__main__":
    configure_logging("etl-pipeline")
    main()
