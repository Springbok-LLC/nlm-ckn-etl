"""Create tuples from NCBI Gene data using schema entities.

Produces Gene → Protein (produces) associations and Gene vertex
annotations from E-Utilities gene data.
"""

import json

from ckn_schema.pydantic.ckn_schema import (
    Gene,
    Protein,
)

from ExternalApiResultsFetcher import GENE_PATH

from LoaderUtilities import (
    get_gene_entrez_id_to_names_map,
    map_gene_entrez_id_to_names,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    remove_protocols,
    write_tuples,
)


def create_tuples(gene_results: dict) -> list[tuple]:
    """Create tuples from NCBI Gene results.

    Produces:
    - GeneProducesProtein
    - Gene vertex annotations
    """
    tuples = []

    gene_entrez_id_to_names = get_gene_entrez_id_to_names_map()
    gene_entrez_ids = gene_results.get("gene_entrez_ids", [])

    for gene_entrez_id in gene_entrez_ids:
        if not gene_results.get(gene_entrez_id):
            continue

        gene_name = map_gene_entrez_id_to_names(
            gene_entrez_id, gene_entrez_id_to_names
        )
        if not gene_name:
            continue
        gene_name = gene_name[0]

        data = gene_results[gene_entrez_id]

        also_known_as = data.get("Also_known_as")
        if isinstance(also_known_as, list):
            also_known_as = ", ".join(str(x) for x in also_known_as)

        mrna_np = data.get("mRNA_(NM)_and_protein_(NP)_sequences")
        if isinstance(mrna_np, list):
            mrna_np = ", ".join(str(x) for x in mrna_np)
        elif mrna_np is not None:
            mrna_np = str(mrna_np)

        gene_entity = Gene(
            gene_symbol=gene_name,
            label=data.get("Official_full_name"),
            gene_type=data.get("Gene_type"),
            gene_id=str(data["Gene_ID"]) if data.get("Gene_ID") is not None else None,
            also_known_as=also_known_as,
            refseq_summary=data.get("Summary"),
            uniprot_name=data.get("UniProt_name"),
            reference_sequence_identifier=data.get("RefSeq_gene_ID"),
            link_to_uniprot_id=remove_protocols(data.get("Link_to_UniProt_ID")),
            species=data.get("Organism"),
            mrna__nm__and_protein__np__sequences=mrna_np,
        )

        # Gene produces Protein
        uniprot_name = data.get("UniProt_name")
        if uniprot_name:
            protein_entity = Protein(
                gene_symbol=gene_name,
                uniprot_id=uniprot_name,
            )
            assoc = ASSOCIATION_CLASSES["GeneProducesProtein"](
                subject=gene_entity,
                predicate="produces",
                object=protein_entity,
            )
            tuples.extend(association_to_tuples(assoc, source="UniProt"))
        else:
            # Still emit Gene annotations even without a Protein association
            from rdflib.term import Literal, URIRef
            from TupleWriterUtilities import entity_to_annotation_triples
            gs_term = f"GS_{gene_name}"
            tuples.extend(entity_to_annotation_triples(gene_entity, gs_term))

    return tuples


def main():
    """Run Gene tuple writer."""
    if not GENE_PATH.exists():
        print(f"Gene results not found at {GENE_PATH}")
        return

    print(f"Creating Gene tuples from {GENE_PATH}")
    with open(GENE_PATH, "r") as fp:
        gene_results = json.load(fp)

    tuples = create_tuples(gene_results)
    if tuples:
        write_tuples(tuples, TUPLES_DIRPATH / "gene.json")


if __name__ == "__main__":
    main()
