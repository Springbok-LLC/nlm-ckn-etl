"""Create tuples from UniProt data using schema entities.

Produces Protein vertex annotations from UniProt API results.
"""

import json

import structlog
from ckn_schema.pydantic.ckn_schema import Protein

from LoaderUtilities import get_current_run
from logging_setup import configure_logging

from TupleWriterUtilities import (
    RejectTally,
    entity_to_annotation_triples,
    get_tuples_dir,
    require_input,
    write_tuples,
)

_events = structlog.get_logger(__name__)


def create_tuples(
    uniprot_results: dict, rejects: RejectTally | None = None
) -> list[tuple]:
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
    rejects : RejectTally, optional
        Collects the accessions with no data; the caller logs it.

    Returns
    -------
    list[tuple]
        List of 3-element annotation triples.
    """
    tuples = []
    rejects = rejects if rejects is not None else RejectTally("uniprot")

    protein_accessions = uniprot_results.get("protein_accessions", [])

    for accession in protein_accessions:
        data = uniprot_results.get(accession)
        if not data:
            rejects.reject("no_data", accession)
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


def main() -> int:
    """Run UniProt tuple writer.

    Loads transformed UniProt results and creates tuples for each
    protein. Writes output to a single JSON tuple file.

    Returns
    -------
    int
        The number of tuples written
    """
    uniprot_path = get_current_run().external_dir / "uniprot_transformed.json"
    require_input(uniprot_path, "UniProt results", "uniprot_transformed")

    with open(uniprot_path, "r") as fp:
        uniprot_results = json.load(fp)
    _events.info(
        "tuple_writer_started",
        source="uniprot",
        input_path=str(uniprot_path),
        records_in=len(uniprot_results.get("protein_accessions", [])),
    )

    rejects = RejectTally("uniprot")
    tuples = create_tuples(uniprot_results, rejects)
    rejects.log()
    if tuples:
        return write_tuples(tuples, get_tuples_dir() / "uniprot.json")
    return 0


if __name__ == "__main__":
    configure_logging("etl-pipeline")
    main()
