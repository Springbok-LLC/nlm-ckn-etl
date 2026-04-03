"""Create tuples from CELLxGENE fetched metadata using schema entities.

Produces CellSetDataset → Publication associations and annotations
from CELLxGENE curation API metadata.
"""

import json

from ckn_schema.pydantic.ckn_schema import (
    CellSetDataset,
    Publication,
)

from ExternalApiResultsFetcher import CELLXGENE_PATH

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
    """
    tuples = []

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
            citation=metadata.get("Citation"),
        )
        pub = Publication(
            pmid=dataset_version_id,
        )

        assoc = ASSOCIATION_CLASSES["CellSetDatasetHasSourcePublication"](
            subject=csd, predicate="source", object=pub,
        )
        tuples.extend(association_to_tuples(assoc, source="CELLxGENE"))

        # Additional PUB annotations not on the Publication entity
        pub_term = f"PUB_{dataset_version_id}"
        from rdflib.term import Literal, URIRef
        from LoaderUtilities import PURLBASE, RDFSBASE
        for key in ["Citation", "Link_to_publication", "Link_to_CELLxGENE_collection"]:
            value = metadata.get(key)
            if value:
                tuples.append(
                    (
                        URIRef(f"{PURLBASE}/{pub_term}"),
                        URIRef(f"{RDFSBASE}#{key}"),
                        Literal(remove_protocols(value)),
                    )
                )

    return tuples


def main():
    """Run CELLxGENE tuple writer."""
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
