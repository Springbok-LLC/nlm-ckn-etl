"""Create tuples from CELLxGENE fetched metadata using schema entities.

Produces CellSetDataset was attributed to Publication associations and
annotations from CELLxGENE curation API metadata.
"""

import json

from ckn_schema.pydantic.ckn_schema import CellSetDataset, Publication
from rdflib.term import Literal, URIRef

from LoaderUtilities import (
    PURLBASE,
    RDFSBASE,
    get_current_run,
    get_dataset_organs_map,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    association_to_tuples,
    cell_set_dataset_identifier,
    cell_set_dataset_name_tuples,
    entity_to_term,
    get_tuples_dir,
    normalize_doi,
    remove_protocols,
    write_tuples,
)


def create_tuples(
    cellxgene_results: dict, dataset_organs: dict | None = None
) -> list[tuple]:
    """Create tuples from CELLxGENE metadata.

    Produces:
    - CellSetDatasetWasAttributedToPublication
    - CSD and PUB vertex annotations

    Publications are keyed by DOI, so the datasets of a paper all share
    one publication vertex.

    A source dataset filtered for several organs yields one cell set dataset
    vertex per organ (matching the summary-driven writers), so the publication
    is attributed to each filtered dataset.  ``dataset_organs`` supplies the
    source-to-organs map; a source absent from it (or with no organ) yields a
    single vertex keyed on the source id alone.

    Parameters
    ----------
    cellxgene_results : dict
        Dictionary of CELLxGENE metadata keyed by dataset_version_id.
        Each value contains Dataset_name, Organism, Tissue,
        Disease_status, Number_of_cells, Citation, and link fields.
    dataset_organs : dict, optional
        Mapping from ``dataset_version_id`` to the set of organs it was
        filtered for, from ``LoaderUtilities.get_dataset_organs_map``.  When
        omitted, every dataset yields a single source-keyed vertex.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []
    dataset_organs = dataset_organs or {}

    # CellSetDataset was_attributed_to Publication
    for dataset_version_id, metadata in cellxgene_results.items():
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
            )
            pub = Publication(
                publication_doi=normalize_doi(metadata.get("Link_to_publication")),
                author_list=metadata.get("Author_list"),
                year=str(metadata.get("Year")),
                title=metadata.get("Title"),
                journal=metadata.get("Journal"),
            )
            ctx = {"dataset_version_id": dataset_version_id}

            assoc = ASSOCIATION_CLASSES["CellSetDatasetWasAttributedToPublication"](
                subject=csd,
                predicate="nlm-ckn:was_attributed_to",
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


def main():
    """Run CELLxGENE tuple writer.

    Loads transformed CELLxGENE metadata and creates tuples for each
    dataset. Writes output to a single JSON tuple file.
    """
    cellxgene_path = get_current_run().external_dir / "cellxgene_transformed.json"
    if not cellxgene_path.exists():
        print(f"CELLxGENE results not found at {cellxgene_path}")
        return

    print(f"Creating CELLxGENE tuples from {cellxgene_path}")
    with open(cellxgene_path, "r") as fp:
        cellxgene_results = json.load(fp)

    dataset_organs = get_dataset_organs_map()
    tuples = create_tuples(cellxgene_results, dataset_organs)
    if tuples:
        write_tuples(tuples, get_tuples_dir() / "cellxgene.json")


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    main()
