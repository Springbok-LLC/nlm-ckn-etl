"""Create tuples from Open Targets data using schema entities.

Produces Disease, Drug, Gene, Mutation, and Protein associations from Open
Targets Platform GraphQL API results.
"""

import json

import pandas as pd
import structlog
from rdflib.term import Literal, URIRef

from ckn_schema.pydantic.ckn_schema import (
    Disease,
    Drug,
    Gene,
    Mutation,
    Protein,
)

from LoaderUtilities import (
    DEPRECATED_TERMS,
    PURLBASE,
    RDFSBASE,
    get_current_run,
    get_efo_to_mondo_map,
    get_gene_ensembl_id_to_names_map,
    get_gene_name_to_entrez_ids_map,
    map_efo_to_mondo,
    map_gene_ensembl_id_to_names,
    map_gene_name_to_entrez_ids,
)
from logging_setup import configure_logging

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    RejectTally,
    association_to_tuples,
    get_tuples_dir,
    require_input,
    write_tuples,
)

_events = structlog.get_logger(__name__)

VALID_PHASES = ["PHASE_3", "APPROVAL"]


def get_mondo_term(
    disease_id: str,
    efo2mondo: pd.DataFrame,
    rejects: RejectTally | None = None,
) -> str | None:
    """Return MONDO term, mapping from EFO when necessary.

    Parameters
    ----------
    disease_id : str
        Disease identifier, either a MONDO or EFO term.
    efo2mondo : pd.DataFrame
        DataFrame indexed by EFO term containing MONDO term mappings.
    rejects : RejectTally, optional
        Where a deprecated term is recorded, as ``deprecated_term``.

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
        if rejects is not None:
            rejects.reject("deprecated_term", mondo_term)
        return None
    return mondo_term


def create_tuples(
    opentargets_results: dict,
    gene_results: dict,
    uniprot_results: dict,
    rejects: RejectTally | None = None,
) -> list[tuple]:
    """Create tuples from Open Targets results.

    Produces:
    - GeneIsAssociatedWithDisease
    - DrugMolecularlyInteractsWithProtein
    - DrugIsSubstanceThatTreatsDisease
    - GeneHasQualityMutation
    - MutationHasPharmacologicalEffectDrug

    Clinical trial ids (NCT) are no longer asserted as a separate
    ClinicalTrial entity/association (ckn-schema removed
    DrugEvaluatedInClinicalTrial in v0.0.0-alpha.6); they are instead
    comma-joined onto Drug.study_id, the schema's replacement slot.

    Parameters
    ----------
    opentargets_results : dict
        Dictionary containing Open Targets results keyed by gene
        Ensembl id, with sub-keys for diseases, drugs, and
        pharmacogenetics.
    gene_results : dict
        Dictionary containing NCBI Gene results keyed by gene Entrez
        id. Used to look up UniProt accessions for protein
        associations.
    uniprot_results : dict
        Dictionary containing UniProt results keyed by protein
        accession. Used to look up protein names for drug targets.
    rejects : RejectTally, optional
        Collects the records dropped or flagged; the caller logs it.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []
    rejects = rejects if rejects is not None else RejectTally("opentargets")

    gene_ensembl_id_to_names = get_gene_ensembl_id_to_names_map()
    gene_name_to_entrez_ids = get_gene_name_to_entrez_ids_map()
    efo2mondo = get_efo_to_mondo_map()

    gene_ensembl_ids = opentargets_results.get("gene_ensembl_ids", [])
    annotated = set()

    for gene_ensembl_id in gene_ensembl_ids:
        gene_name = map_gene_ensembl_id_to_names(
            gene_ensembl_id, gene_ensembl_id_to_names
        )
        if not gene_name:
            rejects.reject("unmapped_ensembl", gene_ensembl_id)
            continue
        gene_name = gene_name[0]
        gene_entrez_id = map_gene_name_to_entrez_ids(gene_name, gene_name_to_entrez_ids)
        if not gene_entrez_id:
            rejects.reject("unmapped_entrez", gene_name)
            continue
        gene_entrez_id = gene_entrez_id[0]

        gene_entity = Gene(gene_symbol=gene_name)

        # Get UniProt accession, and protein name, for protein
        # associations
        uniprot_name = None
        protein_name = None
        gene_data = gene_results.get(gene_entrez_id, {})
        if gene_data.get("UniProt_name"):
            uniprot_name = gene_data["UniProt_name"]
            protein_name = uniprot_results.get(uniprot_name, {}).get("Protein_name")

        ot_data = opentargets_results.get(gene_ensembl_id, {})

        # Gene is_associated_with Disease
        for disease in ot_data.get("diseases", []):
            mondo_term = get_mondo_term(disease["disease"]["id"], efo2mondo, rejects)
            if mondo_term is None or disease["score"] < 0.5:
                continue

            disease_entity = Disease(
                ontology_purl=mondo_term.replace("_", ":"),
                label=disease["disease"].get("name"),
                definition=disease["disease"].get("description"),
            )

            assoc = ASSOCIATION_CLASSES["GeneIsAssociatedWithDisease"](
                subject=gene_entity,
                predicate="nlm-ckn:is_associated_with",
                object=disease_entity,
            )
            tuples.extend(
                association_to_tuples(
                    assoc, source="Open Targets", annotated_terms=annotated
                )
            )

            # Edge annotation: score. Subject/predicate/object must match the
            # relationship triple above so the annotation addresses the same
            # edge (is_associated_with = RO_0004029, since ckn-schema
            # v0.0.0-alpha.6 renamed GeneIsGeneticBasisForDisease).
            gs_term = f"GS_{gene_name}"
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{gs_term}"),
                    URIRef(f"{PURLBASE}/RO_0004029"),
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

            # Collect drug fields
            drug_name = drug["drug"].get("name")
            drug_desc = drug["drug"].get("description")
            drug_type = drug["drug"].get("drugType")
            synonyms = [s["label"] for s in drug["drug"].get("synonyms", [])]
            trade_names_list = [n["label"] for n in drug["drug"].get("tradeNames", [])]

            mechanism = None
            for moa in drug["drug"].get("mechanismsOfAction", {}).get("rows", []):
                if gene_ensembl_id in [t["id"] for t in moa.get("targets", [])]:
                    mechanism = moa.get("mechanismOfAction")
                    break

            # Clinical trial (NCT) ids, collected across every indication before
            # the Drug entity is built -- ckn-schema v0.0.0-alpha.6 dropped the
            # DrugEvaluatedInClinicalTrial association (and its per-report
            # ClinicalTrial entity) in favor of a single Drug.study_id slot.
            # Deduped and ordered: the same trial can appear on more than one
            # indication for the same drug.
            trial_ids = list(
                dict.fromkeys(
                    clinical_report.get("id", "")
                    for indication in (drug["drug"].get("indications") or {}).get(
                        "rows", []
                    )
                    for clinical_report in indication.get("clinicalReports", [])
                    if "nct" in clinical_report.get("id", "").lower()
                )
            )

            drug_entity = Drug(
                label=drug_name,
                definition=drug_desc,
                drug_type=drug_type,
                mechanism_of_action=mechanism,
                trade_names=", ".join(trade_names_list) if trade_names_list else None,
                exact_synonym=", ".join(synonyms) if synonyms else None,
                approval_status=drug["drug"].get("maximumClinicalStage"),
                uniprot_id=uniprot_name,
                protein=protein_name,
                study_id=", ".join(trial_ids) if trial_ids else None,
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
                    predicate="nlm-ckn:molecularly_interacts_with",
                    object=protein_entity,
                )
                tuples.extend(
                    association_to_tuples(
                        assoc,
                        ctx,
                        source="Open Targets and Gene",
                        annotated_terms=annotated,
                    )
                )

            # Drug is_substance_that_treats Disease (from indications)
            if drug["drug"].get("indications"):
                for indication in drug["drug"]["indications"].get("rows", []):
                    mondo_term = get_mondo_term(
                        indication["disease"]["id"], efo2mondo, rejects
                    )
                    if (
                        mondo_term is None
                        or indication.get("maxClinicalStage") not in VALID_PHASES
                    ):
                        continue

                    disease_entity = Disease(
                        ontology_purl=mondo_term.replace("_", ":"),
                        label=indication["disease"].get("name"),
                        definition=indication["disease"].get("description"),
                    )
                    assoc = ASSOCIATION_CLASSES["DrugIsSubstanceThatTreatsDisease"](
                        subject=drug_entity,
                        predicate="nlm-ckn:is_substance_that_treats",
                        object=disease_entity,
                    )
                    tuples.extend(
                        association_to_tuples(
                            assoc, ctx, source="Open Targets", annotated_terms=annotated
                        )
                    )

        # Gene has_quality Mutation, and Mutation has_pharmacological_effect
        # Drug
        for pg in ot_data.get("pharmacogenetics", []):
            variant_rs_id = pg.get("variantRsId")
            if variant_rs_id is None:
                rejects.reject("missing_rsid", gene_name)
                continue

            so_term = pg.get("variantFunctionalConsequenceId")
            if so_term in DEPRECATED_TERMS:
                rejects.flag("deprecated_term", so_term)

            mutation_entity = Mutation(
                reference_sequence_identifier=variant_rs_id,
                genotype_id=str(pg.get("genotypeId")),
                genotype=str(pg.get("genotype")),
                phenotype=str(pg.get("phenotypeText")),
                genotype_annotation=str(pg.get("genotypeAnnotationText")),
                evidence_level=str(pg.get("evidenceLevel")),
                publication=str(pg.get("literature")),
            )

            # Gene has_quality Mutation
            assoc = ASSOCIATION_CLASSES["GeneHasQualityMutation"](
                subject=gene_entity,
                predicate="nlm-ckn:has_quality",
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
                    rejects.reject("missing_drug_id", variant_rs_id)
                    continue
                pg_drug_entity = Drug(label=pg_drug.get("drugFromSource", drug_id))
                pg_chembl_id = drug_id.replace("CHEMBL", "")
                assoc = ASSOCIATION_CLASSES["MutationHasPharmacologicalEffectDrug"](
                    subject=mutation_entity,
                    predicate="nlm-ckn:has_pharmacological_effect",
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


def main() -> int:
    """Run Open Targets tuple writer.

    Loads transformed Open Targets, Gene, and UniProt results and
    creates tuples for each target, disease, drug, and pharmacogenetic
    resource. Writes output to a single JSON tuple file.

    Returns
    -------
    int
        The number of tuples written
    """
    external_dir = get_current_run().external_dir
    opentargets_path = external_dir / "opentargets_transformed.json"
    gene_path = external_dir / "gene_transformed.json"
    uniprot_path = external_dir / "uniprot_transformed.json"
    require_input(
        opentargets_path, "Open Targets results", "opentargets_transformed"
    )
    require_input(gene_path, "Gene results", "gene_transformed")
    require_input(uniprot_path, "UniProt results", "uniprot_transformed")

    with open(opentargets_path, "r") as fp:
        opentargets_results = json.load(fp)
    with open(gene_path, "r") as fp:
        gene_results = json.load(fp)
    with open(uniprot_path, "r") as fp:
        uniprot_results = json.load(fp)

    _events.info(
        "tuple_writer_started",
        source="opentargets",
        input_path=str(opentargets_path),
        records_in=len(opentargets_results.get("gene_ensembl_ids", [])),
    )

    rejects = RejectTally("opentargets")
    try:
        tuples = create_tuples(opentargets_results, gene_results, uniprot_results, rejects)
    finally:
        rejects.log()
    if tuples:
        return write_tuples(tuples, get_tuples_dir() / "opentargets.json")
    return 0


if __name__ == "__main__":
    configure_logging("etl-pipeline")
    main()
