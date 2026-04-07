"""Create tuples from Open Targets data using schema entities.

Produces ClinicalTrial, Disease, Drug, Gene, Mutation, and Protein
associations from Open Targets Platform GraphQL API results.
"""

import json

import pandas as pd
from rdflib.term import Literal, URIRef

from ckn_schema.pydantic.ckn_schema import (
    ClinicalTrial,
    Disease,
    Drug,
    Gene,
    Mutation,
    Protein,
)

from ExternalApiResultsFetcher import (
    OPENTARGETS_PATH,
    GENE_PATH,
)

from LoaderUtilities import (
    DEPRECATED_TERMS,
    PURLBASE,
    RDFSBASE,
    get_chembl_to_pubchem_map,
    get_efo_to_mondo_map,
    get_gene_ensembl_id_to_names_map,
    get_gene_name_to_entrez_ids_map,
    map_chembl_to_pubchem,
    map_efo_to_mondo,
    map_gene_ensembl_id_to_names,
    map_gene_name_to_entrez_ids,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    remove_protocols,
    write_tuples,
)

VALID_PHASES = ["PHASE_3", "APPROVAL"]


def get_mondo_term(disease_id: str, efo2mondo: pd.DataFrame) -> str | None:
    """Return MONDO term, mapping from EFO when necessary.

    Parameters
    ----------
    disease_id : str
        Disease identifier, either a MONDO or EFO term.
    efo2mondo : pd.DataFrame
        DataFrame indexed by EFO term containing MONDO term mappings.

    Returns
    -------
    str or None
        MONDO term, or None if the term is deprecated or unmappable.
    """
    mondo_term = None
    if "MONDO" in disease_id:
        mondo_term = disease_id
    elif "EFO" in disease_id:
        mondo_term = map_efo_to_mondo(disease_id, efo2mondo)
    if mondo_term in DEPRECATED_TERMS:
        print(f"Warning: MONDO term {mondo_term} deprecated")
        return None
    return mondo_term


def create_tuples(opentargets_results: dict, gene_results: dict) -> list[tuple]:
    """Create tuples from Open Targets results.

    Produces:
    - GeneIsGeneticBasisForDisease
    - DrugMolecularlyInteractsWithProtein
    - DrugIsSubstanceThatTreatsDisease
    - DrugEvaluatedInClinicalTrial
    - GeneMolecularlyInteractsWithDrug
    - DrugMolecularlyInteractsWithGene
    - GeneGeneticallyInteractsWithGene
    - GeneHasQualityMutation
    - MutationHasPharamcologicalEffectDrug

    Parameters
    ----------
    opentargets_results : dict
        Dictionary containing Open Targets results keyed by gene
        Ensembl id, with sub-keys for diseases, drugs, interactions,
        pharmacogenetics, tractability, expression, and depmap.
    gene_results : dict
        Dictionary containing NCBI Gene results keyed by gene Entrez
        id. Used to look up UniProt names for protein associations.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []

    gene_ensembl_id_to_names = get_gene_ensembl_id_to_names_map()
    gene_name_to_entrez_ids = get_gene_name_to_entrez_ids_map()
    efo2mondo = get_efo_to_mondo_map()
    chembl2pubchem = get_chembl_to_pubchem_map()

    gene_ensembl_ids = opentargets_results.get("gene_ensembl_ids", [])
    annotated = set()

    for gene_ensembl_id in gene_ensembl_ids:
        gene_name = map_gene_ensembl_id_to_names(
            gene_ensembl_id, gene_ensembl_id_to_names
        )
        if not gene_name:
            print(f"Warning: Cannot map Ensembl ID {gene_ensembl_id} to gene name")
            continue
        gene_name = gene_name[0]
        gene_entrez_id = map_gene_name_to_entrez_ids(gene_name, gene_name_to_entrez_ids)
        if not gene_entrez_id:
            print(f"Warning: Cannot map gene name {gene_name} to Entrez ID")
            continue
        gene_entrez_id = gene_entrez_id[0]

        gene_entity = Gene(gene_symbol=gene_name)

        # Get UniProt name for protein associations
        uniprot_name = None
        if (
            gene_entrez_id in gene_results
            and "UniProt_name" in gene_results[gene_entrez_id]
            and gene_results[gene_entrez_id]["UniProt_name"]
        ):
            uniprot_name = gene_results[gene_entrez_id]["UniProt_name"]

        ot_data = opentargets_results.get(gene_ensembl_id, {})

        # Gene is_genetic_basis_for_condition Disease
        for disease in ot_data.get("diseases", []):
            mondo_term = get_mondo_term(disease["disease"]["id"], efo2mondo)
            if mondo_term is None or disease["score"] < 0.5:
                continue

            disease_entity = Disease(
                ontology_purl=mondo_term,
                label=disease["disease"].get("name"),
                definition=disease["disease"].get("description"),
            )

            assoc = ASSOCIATION_CLASSES["GeneIsGeneticBasisForDisease"](
                subject=gene_entity,
                predicate="is_genetic_basis_for_condition",
                object=disease_entity,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, source="Open Targets", annotated_terms=annotated
                )
            )

            # Edge annotation: score
            gs_term = f"GS_{gene_name}"
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/RO_0004010"),
                    URIRef(f"{PURLBASE}/{mondo_term}"),
                    URIRef(f"{RDFSBASE}#Score"),
                    Literal(str(disease["score"])),
                )
            )

        # --- Drugs ---
        for drug in ot_data.get("drugs", []):
            if drug["drug"]["maximumClinicalStage"] not in VALID_PHASES:
                continue
            if any(
                w["warningType"] == "Withdrawn"
                for w in drug["drug"].get("drugWarnings", [])
            ):
                continue

            chembl_id = drug["drug"]["id"].replace("CHEMBL", "")
            chembl_term = f"CHEMBL_{chembl_id}"

            # Collect drug fields
            drug_name = drug["drug"].get("name")
            drug_desc = drug["drug"].get("description")
            drug_type = drug["drug"].get("drugType")
            synonyms = drug["drug"].get("synonyms", [])
            trade_names_list = drug["drug"].get("tradeNames", [])

            mechanism = None
            for moa in drug["drug"].get("mechanismsOfAction", {}).get("rows", []):
                if gene_ensembl_id in [t["id"] for t in moa.get("targets", [])]:
                    mechanism = moa.get("mechanismOfAction")
                    break

            pubchem_id = map_chembl_to_pubchem(
                chembl_term.replace("_", ""), chembl2pubchem
            )
            link_to_uniprot = None
            if gene_entrez_id in gene_results:
                link_to_uniprot = remove_protocols(
                    gene_results[gene_entrez_id].get("Link_to_UniProt_ID")
                )

            drug_entity = Drug(
                drug_name=drug_name,
                drug_description=drug_desc,
                drug_type=drug_type,
                mechanism_of_action=mechanism,
                trade_names=", ".join(trade_names_list) if trade_names_list else None,
                exact_synonym=", ".join(synonyms) if synonyms else None,
                approval_status=drug["drug"].get("maximumClinicalStage"),
                link_to_pubchem_record=(
                    f"pubchem.ncbi.nlm.nih.gov/compound/{pubchem_id}"
                    if pubchem_id
                    else None
                ),
                link_to_uniprot_id=link_to_uniprot,
                uniprot_id=uniprot_name,
                protein=gene_name,
            )
            ctx = {"chembl_id": chembl_id}

            # Drug molecularly_interacts_with Protein
            if uniprot_name:
                protein_entity = Protein(
                    gene_symbol=gene_name,
                    uniprot_id=uniprot_name,
                )
                assoc = ASSOCIATION_CLASSES["DrugMolecularlyInteractsWithProtein"](
                    subject=drug_entity,
                    predicate="molecularly_interacts_with",
                    object=protein_entity,
                )
                tuples.extend(
                    association_to_tuples(
                        assoc,
                        ctx,
                        source="Open Targets and UniProt",
                        annotated_terms=annotated,
                    )
                )

            # Drug is_substance_that_treats Disease (from indications)
            if drug["drug"].get("indications"):
                for indication in drug["drug"]["indications"].get("rows", []):
                    mondo_term = get_mondo_term(indication["disease"]["id"], efo2mondo)
                    if (
                        mondo_term is None
                        or indication.get("maxClinicalStage") not in VALID_PHASES
                    ):
                        continue

                    disease_entity = Disease(
                        ontology_purl=mondo_term,
                        label=indication["disease"].get("name"),
                        definition=indication["disease"].get("description"),
                    )
                    assoc = ASSOCIATION_CLASSES["DrugIsSubstanceThatTreatsDisease"](
                        subject=drug_entity,
                        predicate="is_substance_that_treats",
                        object=disease_entity,
                    )
                    tuples.extend(
                        association_to_tuples(
                            assoc, ctx, source="Open Targets", annotated_terms=annotated
                        )
                    )

                    # Drug evaluated_in ClinicalTrial
                    for clinical_report in indication.get("clinicalReports", []):
                        trial_id = clinical_report.get("id", "")
                        if "nct" not in trial_id.lower():
                            continue
                        ct_entity = ClinicalTrial(study_id=trial_id)
                        assoc = ASSOCIATION_CLASSES["DrugEvaluatedInClinicalTrial"](
                            subject=drug_entity,
                            predicate="evaluated_in",
                            object=ct_entity,
                        )
                        tuples.extend(
                            association_to_tuples(
                                assoc,
                                ctx,
                                source="Open Targets",
                                annotated_terms=annotated,
                            )
                        )

        # Gene molecularly_interacts_with Drug, and Drug
        # molecularly_interacts_with Gene
        for drug in ot_data.get("drugs", []):
            if drug["drug"]["maximumClinicalStage"] not in VALID_PHASES:
                continue
            if any(
                w["warningType"] == "Withdrawn"
                for w in drug["drug"].get("drugWarnings", [])
            ):
                continue

            chembl_id = drug["drug"]["id"].replace("CHEMBL", "")
            drug_name = drug["drug"].get("name")
            drug_entity_sym = Drug(drug_name=drug_name)
            ctx_sym = {"chembl_id": chembl_id}

            # Gene molecularly_interacts_with Drug
            assoc = ASSOCIATION_CLASSES["GeneMolecularlyInteractsWithDrug"](
                subject=gene_entity,
                predicate="molecularly_interacts_with",
                object=drug_entity_sym,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx_sym, source="Open Targets", annotated_terms=annotated
                )
            )

            # Drug molecularly_interacts_with Gene
            assoc = ASSOCIATION_CLASSES["DrugMolecularlyInteractsWithGene"](
                subject=drug_entity_sym,
                predicate="molecularly_interacts_with",
                object=gene_entity,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, ctx_sym, source="Open Targets", annotated_terms=annotated
                )
            )

        # Gene genetically_interacts_with Gene
        for interaction in ot_data.get("interactions", []):
            target_b = interaction.get("targetB")
            if target_b is None:
                print(f"Warning: Missing interaction target for gene {gene_name}")
                continue
            gene_b_symbol = target_b.get("approvedSymbol")
            if not gene_b_symbol:
                print(
                    f"Warning: No approved symbol for interaction target of gene {gene_name}"
                )
                continue
            gene_b_entity = Gene(gene_symbol=gene_b_symbol)
            assoc = ASSOCIATION_CLASSES["GeneGeneticallyInteractsWithGene"](
                subject=gene_entity,
                predicate="genetically_interacts_with",
                object=gene_b_entity,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, source="Open Targets", annotated_terms=annotated
                )
            )

        # Gene has_quality Mutation, and Mutation has_pharmacological_effect
        # Drug
        for pg in ot_data.get("pharmacogenetics", []):
            variant_rs_id = pg.get("variantRsId")
            if variant_rs_id is None:
                print(
                    f"Warning: Missing variant RS ID in pharmacogenetics for gene {gene_name}"
                )
                continue

            so_term = pg.get("variantFunctionalConsequenceId")
            if so_term in DEPRECATED_TERMS:
                print(f"Warning: SO term {so_term} deprecated")

            mutation_entity = Mutation(
                reference_sequence_identifier=variant_rs_id,
                genotype_id=str(pg.get("genotypeId")),
                genotype=str(pg.get("genotype")),
                phenotype=str(pg.get("phenotypeText")),
                genotype_annotation=str(pg.get("genotypeAnnotationText")),
                evidence_level=str(pg.get("evidenceLevel")),
                source=str(pg.get("datasourceId")),
                literature=str(pg.get("literature")),
            )

            # Gene has_quality Mutation
            assoc = ASSOCIATION_CLASSES["GeneHasQualityMutation"](
                subject=gene_entity,
                predicate="has_quality",
                object=mutation_entity,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, source="Open Targets", annotated_terms=annotated
                )
            )

            # TODO: Remove
            # VariantConsequence annotation (manual — no association class for
            # Mutation→VariantConsequence in the schema yet)
            if so_term:
                rs_term = variant_rs_id.replace("rs", "RS_")
                tuples.append(
                    (
                        URIRef(f"{PURLBASE}/{rs_term}"),
                        URIRef(f"{PURLBASE}/RO_0002331"),
                        URIRef(f"{PURLBASE}/{so_term}"),
                    )
                )
                tuples.append(
                    (
                        URIRef(f"{PURLBASE}/{rs_term}"),
                        URIRef(f"{PURLBASE}/RO_0002331"),
                        URIRef(f"{PURLBASE}/{so_term}"),
                        URIRef(f"{RDFSBASE}#Source"),
                        Literal("Open Targets"),
                    )
                )
                vc_label = pg.get("variantFunctionalConsequence")
                if vc_label:
                    tuples.append(
                        (
                            URIRef(f"{PURLBASE}/{so_term}"),
                            URIRef(f"{RDFSBASE}#Variant_consequence_label"),
                            Literal(str(vc_label)),
                        )
                    )

            # Mutation has_pharmacological_effect Drug
            for pg_drug in pg.get("drugs", []):
                drug_id = pg_drug.get("drugId")
                if drug_id is None:
                    print(
                        f"Warning: Missing drug ID in pharmacogenetics for variant {variant_rs_id}"
                    )
                    continue
                pg_drug_entity = Drug(drug_name=pg_drug.get("drugFromSource", drug_id))
                pg_chembl_id = drug_id.replace("CHEMBL", "")
                assoc = ASSOCIATION_CLASSES["MutationHasPharamcologicalEffectDrug"](
                    subject=mutation_entity,
                    predicate="has_pharmacological_effect",
                    object=pg_drug_entity,
                )
                tuples.extend(
                    association_to_tuples(
                        assoc,
                        {"chembl_id": pg_chembl_id},
                        source="Open Targets",
                        annotated_terms=annotated,
                    )
                )

    return tuples


def main():
    """Run Open Targets tuple writer.

    Loads Open Targets and Gene results from their fetched JSON files and
    creates tuples for each target, disease, drug, interaction, and
    pharmacogenetic resource. Writes output to a single JSON tuple file.
    """
    if not OPENTARGETS_PATH.exists():
        print(f"Open Targets results not found at {OPENTARGETS_PATH}")
        return
    if not GENE_PATH.exists():
        print(f"Gene results not found at {GENE_PATH}")
        return

    print(f"Creating Open Targets tuples from {OPENTARGETS_PATH}")
    with open(OPENTARGETS_PATH, "r") as fp:
        opentargets_results = json.load(fp)
    with open(GENE_PATH, "r") as fp:
        gene_results = json.load(fp)

    tuples = create_tuples(opentargets_results, gene_results)
    if tuples:
        write_tuples(tuples, TUPLES_DIRPATH / "opentargets.json")


if __name__ == "__main__":
    main()
