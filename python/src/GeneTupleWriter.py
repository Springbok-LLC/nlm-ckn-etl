"""Create tuples from NCBI Gene data using schema entities.

Produces Gene produces Protein associations and Gene vertex annotations from
E-Utilities gene data.
"""

import json

from ckn_schema.pydantic.ckn_schema import Gene, Protein

from DataFetcher import GENE_PATH

from LoaderUtilities import (
    get_gene_entrez_id_to_names_map,
    map_gene_entrez_id_to_names,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    entity_to_annotation_triples,
    remove_protocols,
    write_tuples,
)


def create_tuples(gene_results: dict) -> list[tuple]:
    """Create tuples from NCBI Gene results.

    Produces:
    - GeneProducesProtein
    - Gene vertex annotations

    Parameters
    ----------
    gene_results : dict
        Dictionary containing NCBI Gene results keyed by gene Entrez
        id, with a 'gene_entrez_ids' key listing all ids. Each gene
        entry contains fields such as Gene_ID, Official_full_name,
        Gene_type, UniProt_name, Also_known_as, Summary, etc.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []

    gene_entrez_id_to_names = get_gene_entrez_id_to_names_map()
    gene_entrez_ids = gene_results.get("gene_entrez_ids", [])

    for gene_entrez_id in gene_entrez_ids:
        if not gene_results.get(gene_entrez_id):
            print(f"Warning: No data for gene Entrez ID {gene_entrez_id}")
            continue

        gene_name = map_gene_entrez_id_to_names(gene_entrez_id, gene_entrez_id_to_names)
        if not gene_name:
            print(f"Warning: Cannot map gene Entrez ID {gene_entrez_id} to name")
            continue
        gene_name = gene_name[0]

        data = gene_results[gene_entrez_id]

        exact_synonym = data.get("Also_known_as")
        if isinstance(exact_synonym, list):
            exact_synonym = ", ".join(str(a) for a in exact_synonym)

        mrna_pro_seq = data.get("mRNA_(NM)_and_protein_(NP)_sequences")
        if isinstance(mrna_pro_seq, list):
            mrna_pro_seq = ", ".join(str(x) for x in mrna_pro_seq)
        elif mrna_pro_seq is not None:
            mrna_pro_seq = str(mrna_pro_seq)

        gene_entity = Gene(
            gene_symbol=gene_name,
            label=data.get("Official_full_name"),
            gene_type=data.get("Gene_type"),
            gene_id=str(data["Gene_ID"]) if data.get("Gene_ID") is not None else None,
            exact_synonym=exact_synonym,
            refseq_summary=data.get("Summary"),
            uniprot_name=data.get("UniProt_name"),
            reference_sequence_identifier=data.get("RefSeq_gene_ID"),
            species=data.get("Organism"),
            mrna__nm__and_protein__np__sequences=mrna_pro_seq,
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
            gs_term = f"GS_{gene_name}"
            tuples.extend(entity_to_annotation_triples(gene_entity, gs_term))

    return tuples


def main():
    """Run Gene tuple writer.

    Loads NCBI Gene results from the fetched JSON file and creates tuples for
    each gene and its associated protein. Writes output to a single JSON tuple
    file.
    """
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
