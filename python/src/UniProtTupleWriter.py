"""Create tuples from UniProt data using schema entities.

Produces Protein vertex annotations from UniProt API results.
"""

import json

from ckn_schema.pydantic.ckn_schema import Protein

from DataFetcher import UNIPROT_PATH

from TupleWriterUtilities import (
    TUPLES_DIRPATH,
    entity_to_annotation_triples,
    write_tuples,
)


def create_tuples(uniprot_results: dict) -> list[tuple]:
    """Create tuples from UniProt results.

    Produces Protein vertex annotations.

    Parameters
    ----------
    uniprot_results : dict
        Dictionary containing UniProt results keyed by protein
        accession, with a 'protein_accessions' key listing all
        accessions. Each entry contains Protein_name, UniProt_ID,
        Gene_name, Number_of_amino_acids, Function, Annotation_score,
        and Organism.

    Returns
    -------
    list[tuple]
        List of 3-element annotation triples.
    """
    tuples = []

    protein_accessions = uniprot_results.get("protein_accessions", [])

    for accession in protein_accessions:
        data = uniprot_results.get(accession)
        if not data:
            continue

        ann_score = data.get("Annotation_score")
        protein_entity = Protein(
            gene_symbol=data.get("Gene_name", accession),
            uniprot_id=data.get("UniProt_ID", accession),
            label=data.get("Protein_name"),
            number_of_amino_acids=(
                int(data["Number_of_amino_acids"])
                if data.get("Number_of_amino_acids")
                else None
            ),
            protein_function=data.get("Function"),
            species=data.get("Organism"),
            annotation_score=int(ann_score) if ann_score is not None else None,
        )

        pr_term = f"PR_{accession}"
        tuples.extend(entity_to_annotation_triples(protein_entity, pr_term))

    return tuples


def main():
    """Run UniProt tuple writer.

    Loads UniProt results from the fetched JSON file and creates
    Protein vertex annotations. Writes output to a single JSON tuple
    file.
    """
    if not UNIPROT_PATH.exists():
        print(f"UniProt results not found at {UNIPROT_PATH}")
        return

    print(f"Creating UniProt tuples from {UNIPROT_PATH}")
    with open(UNIPROT_PATH, "r") as fp:
        uniprot_results = json.load(fp)

    tuples = create_tuples(uniprot_results)
    if tuples:
        write_tuples(tuples, TUPLES_DIRPATH / "uniprot.json")


if __name__ == "__main__":
    main()
