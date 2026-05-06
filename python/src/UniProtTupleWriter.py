"""Create tuples from UniProt data using schema entities.

Produces Protein vertex annotations from UniProt API results.
"""

import json

from ckn_schema.pydantic.ckn_schema import Protein

from LoaderUtilities import get_current_run

from TupleWriterUtilities import (
    entity_to_annotation_triples,
    get_tuples_dir,
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
            print(f"Warning: No data for protein accession {accession}")
            continue

        ann_score = data.get("Annotation_score")
        protein_entity = Protein(
            gene_symbol=data.get("Gene_name") or accession,
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

    Loads transformed UniProt results and creates tuples for each
    protein. Writes output to a single JSON tuple file.
    """
    uniprot_path = get_current_run().external_dir / "uniprot_transformed.json"
    if not uniprot_path.exists():
        print(f"UniProt results not found at {uniprot_path}")
        return

    print(f"Creating UniProt tuples from {uniprot_path}")
    with open(uniprot_path, "r") as fp:
        uniprot_results = json.load(fp)

    tuples = create_tuples(uniprot_results)
    if tuples:
        write_tuples(tuples, get_tuples_dir() / "uniprot.json")


if __name__ == "__main__":
    main()
