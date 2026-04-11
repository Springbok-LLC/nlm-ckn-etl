"""Create tuples from CELLxGENE fetched metadata using schema entities.

Produces CellSetDataset has source Publication associations and annotations
from CELLxGENE curation API metadata.
"""

import json

from ckn_schema.pydantic.ckn_schema import CellSetDataset, Publication
from rdflib.term import Literal, URIRef

from DataFetcher import CELLXGENE_PATH

from LoaderUtilities import PURLBASE, RDFSBASE

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    remove_protocols,
    write_tuples,
)


def create_tuples(cellxgene_results: dict) -> list[tuple]:
    """Create tuples from CELLxGENE metadata.

    Produces:
    - CellSetDatasetHasSourcePublication
    - CSD and PUB vertex annotations

    Parameters
    ----------
    cellxgene_results : dict
        Dictionary of CELLxGENE metadata keyed by dataset_version_id.
        Each value contains Dataset_name, Organism, Tissue,
        Disease_status, Number_of_cells, Citation, and link fields.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []

    # CellSetDataset source Publication
    for dataset_version_id, metadata in cellxgene_results.items():
        csd = CellSetDataset(
            dataset_identifier=dataset_version_id,
            dataset_name=metadata.get("Dataset_name"),
            species=metadata.get("Organism"),
            anatomical_structure=metadata.get("Tissue"),
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
            publication_doi=remove_protocols(metadata.get("Link_to_publication")),
        )
        ctx = {"dataset_version_id": dataset_version_id}

        assoc = ASSOCIATION_CLASSES["CellSetDatasetHasSourcePublication"](
            subject=csd,
            predicate="source",
            object=pub,
        )
        tuples.extend(
            association_to_tuples(assoc, ctx, source="CELLxGENE")
        )

        # Additional PUB annotations not on the Publication entity
        pub_term = f"PUB_{dataset_version_id}"

        citation = metadata.get("Citation")
        if citation:
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{pub_term}"),
                    URIRef(f"{RDFSBASE}#Citation"),
                    Literal(citation),
                )
            )

    return tuples


def main():
    """Run CELLxGENE tuple writer.

    Loads CELLxGENE metadata from the fetched JSON file and creates tuples for
    each dataset. Writes output to a single JSON tuple file.
    """
    if not CELLXGENE_PATH.exists():
        print(f"CELLxGENE results not found at {CELLXGENE_PATH}")
        return

    print(f"Creating CELLxGENE tuples from {CELLXGENE_PATH}")
    with open(CELLXGENE_PATH, "r") as fp:
        cellxgene_results = json.load(fp)

    tuples = create_tuples(cellxgene_results)
    if tuples:
        write_tuples(tuples, TUPLES_DIRPATH / "cellxgene.json")


if __name__ == "__main__":
    main()
