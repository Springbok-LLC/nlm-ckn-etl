"""Shared infrastructure for schema-based tuple writers.

Provides constants, helper functions, entity factories, and generic
tuple-generation functions used by all data-source-specific tuple
writer modules.
"""

import ast
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
from rdflib.term import Literal, URIRef

import ckn_schema.pydantic.ckn_schema as ckn_module
from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    Association,
    BinaryGeneSet,
    BiomarkerCombination,
    CellSet,
    CellSetDataset,
    CellType,
    ClinicalTrial,
    Disease,
    Drug,
    Gene,
    Mutation,
    Protein,
    Publication,
    VariantConsequence,
)

from LoaderUtilities import (
    DATA_DIRPATH,
    PURLBASE,
    RDFSBASE,
    hyphenate,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TUPLES_DIRPATH = DATA_DIRPATH / "tuples"

# Maps subproperty_of name (from Association linkml_meta) to OBO URI term.
PREDICATE_MAP: dict[str, str] = {
    "part_of": "BFO_0000050",
    "derives_from": "RO_0001000",
    "develops_from": "RO_0002202",
    "composed_primarily_of": "RO_0002473",
    "expresses": "RO_0002292",
    "selectively_expresses": "RO_0002294",
    "has_characterizing_marker_set": "RO_0015004",
    "has_exemplar_data": "RO_0015001",
    "subcluster_of": "RO_0015003",
    "subclass_of": "rdfs_subClassOf",
    "source": "dc#Source",
    "produces": "RO_0003000",
    "interacts_with": "RO_0002434",
    "molecularly_interacts_with": "RO_0002436",
    "genetically_interacts_with": "RO_0002435",
    "is_genetic_basis_for_condition": "RO_0004010",
    "is_substance_that_treats": "RO_0002606",
    "has_quality": "RO_0000086",
    "has_pharmacological_effect": "RO_0002027",
    "has_plasma_membrane_part": "RO_0002104",
    "lacks_plasma_membrane_part": "CL_4030046",
    "capable_of": "RO_0002215",
    "involved_in": "RO_0002331",
    "located_in": "RO_0001025",
    "exact_match": "SKOS_exactMatch",
    "evaluated_in": "RO_0020325",
}

# Maps Pydantic field names to annotation attribute names where the
# default capitalization convention does not match.
FIELD_NAME_MAP: dict[str, str] = {
    "f_beta_score": "F_beta_confidence_score",
    "cell_count": "Total_cell_count",
    "publication_doi": "DOI",
    "drug_name": "Name",
    "drug_description": "Description",
    "drug_type": "Type",
    "gene_id": "Gene_ID",
    "gene_type": "Gene_type",
    "uniprot_name": "UniProt_name",
    "protein_function": "Function",
    "number_of_amino_acids": "Number_of_amino_acids",
    "annotation_score": "Annotation_score",
    "variant_consequence_label": "Variant_consequence_label",
    "silhouette_score": "Silhouette_score",
    "collection_id": "Collection_ID",
    "dataset_name": "Dataset_name",
    "disease_status": "Disease_status",
}

# Fields to skip when generating vertex annotation triples because
# their value is already encoded in the vertex term itself.
TERM_ENCODED_FIELDS: dict[str, set[str]] = {
    "CellType": {"ontology_purl"},
    "AnatomicalStructure": {"ontology_purl"},
    "Disease": {"ontology_purl"},
    "BiologicalProcess": {"ontology_purl"},
    "CellularComponent": {"ontology_purl"},
    "MolecularFunction": {"ontology_purl"},
    "Species": {"ontology_purl"},
    "LifeCycleStage": {"ontology_purl"},
    "Gene": {"gene_symbol"},
    "Protein": {"uniprot_id"},
    "CellSetDataset": {"dataset_identifier"},
    "Publication": {"pmid"},
    "ClinicalTrial": {"study_id"},
    "Mutation": {"reference_sequence_identifier"},
    "VariantConsequence": {"ontology_purl"},
}

# Entity fields that become edge annotation quintuples rather than
# vertex annotation triples.
EDGE_ANNOTATION_FIELDS: dict[str, dict[str, set[str]]] = {
    "CellSetHasCharacterizingMarkerSetBiomarkerCombination": {
        "object": {"f_beta_score"},
    },
}

# Auto-discover Association subclasses from the schema module.
ASSOCIATION_CLASSES: dict[str, type] = {
    name: cls
    for name, cls in vars(ckn_module).items()
    if isinstance(cls, type) and issubclass(cls, Association) and cls is not Association
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def purl_to_curie(purl: str) -> str:
    """Convert an OBO PURL to a CURIE.

    "http://purl.obolibrary.org/obo/CL_0000235"  -> "CL:0000235"
    """
    m = re.match(r"https?://purl\.obolibrary\.org/obo/(\w+?)_(\d+)$", purl)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return purl


def parse_string_list(s: str) -> list[str]:
    """Parse a stringified Python list."""
    try:
        result = ast.literal_eval(s)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (ValueError, SyntaxError):
        pass
    return []


def curie_to_term(curie: str) -> str:
    """Convert a CURIE like 'CL:0000235' to 'CL_0000235'."""
    return curie.replace(":", "_")


def remove_protocols(value):
    """Remove http:// and https:// protocols from a string value."""
    if isinstance(value, str):
        value = value.replace("http://", "")
        value = value.replace("https://", "")
    return value


# ---------------------------------------------------------------------------
# Tuple infrastructure
# ---------------------------------------------------------------------------


def get_predicate_uri(association: Association) -> URIRef:
    """Extract the RO/BFO predicate URI from an Association's linkml_meta."""
    cls = type(association)
    meta = cls.model_fields["predicate"].json_schema_extra or {}
    linkml_meta = meta.get("linkml_meta", {})
    subprop = linkml_meta.get("subproperty_of")
    if subprop is None:
        raise ValueError(
            f"{cls.__name__} predicate has no subproperty_of in linkml_meta"
        )
    obo_term = PREDICATE_MAP.get(subprop)
    if obo_term is None:
        raise ValueError(
            f"No PREDICATE_MAP entry for subproperty_of={subprop!r} "
            f"(Association: {cls.__name__})"
        )
    if subprop == "source":
        return URIRef(f"{RDFSBASE}/dc#Source")
    return URIRef(f"{PURLBASE}/{obo_term}")


def entity_to_term(entity: Any, context: dict[str, Any] | None = None) -> str | None:
    """Convert a Pydantic entity instance to an ArangoDB vertex term."""
    ctx = context or {}

    if isinstance(entity, CellType):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None

    if isinstance(entity, AnatomicalStructure):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None

    if isinstance(entity, Gene):
        return f"GS_{entity.gene_symbol}"

    if isinstance(entity, Protein):
        uid = getattr(entity, "uniprot_id", None)
        return f"PR_{uid}" if uid else None

    if isinstance(entity, CellSet):
        name = getattr(entity, "author_cell_term", None)
        uuid = ctx.get("uuid")
        if name and uuid:
            return f"CS_{name}-{uuid}"
        return None

    if isinstance(entity, CellSetDataset):
        did = getattr(entity, "dataset_identifier", None)
        return f"CSD_{did}" if did else None

    if isinstance(entity, BiomarkerCombination):
        uuid = ctx.get("uuid")
        return f"BMC_{uuid}" if uuid else None

    if isinstance(entity, BinaryGeneSet):
        uuid = ctx.get("uuid")
        return f"BGS_{uuid}" if uuid else None

    if isinstance(entity, Publication):
        pmid = getattr(entity, "pmid", None)
        return f"PUB_{pmid}" if pmid else None

    if isinstance(entity, Drug):
        chembl_id = ctx.get("chembl_id")
        if chembl_id:
            return f"CHEMBL_{chembl_id}"
        drug_name = getattr(entity, "drug_name", None)
        return f"DRUG_{drug_name}" if drug_name else None

    if isinstance(entity, ClinicalTrial):
        sid = getattr(entity, "study_id", None)
        if sid:
            return sid.replace("NCT", "NCT_").replace("nct", "NCT_")
        return None

    if isinstance(entity, Mutation):
        rsid = getattr(entity, "reference_sequence_identifier", None)
        if rsid:
            return rsid.replace("rs", "RS_")
        return None

    # Fallback for ontology-purl entities (Disease, BiologicalProcess, etc.)
    purl = getattr(entity, "ontology_purl", None)
    if purl:
        return curie_to_term(purl)

    return None


def _format_field_name(field_name: str) -> str:
    """Format a Pydantic field name as an annotation attribute name."""
    if field_name in FIELD_NAME_MAP:
        return FIELD_NAME_MAP[field_name]
    return field_name[0].upper() + field_name[1:]


def entity_to_annotation_triples(
    entity: Any,
    term: str,
    edge_fields: set[str] | None = None,
) -> list[tuple]:
    """Generate vertex annotation triples for all populated fields on
    an entity.
    """
    cls = type(entity)
    if not hasattr(cls, "model_fields"):
        return []

    triples = []
    cls_name = cls.__name__
    skip_fields = TERM_ENCODED_FIELDS.get(cls_name, set())
    if edge_fields:
        skip_fields = skip_fields | edge_fields

    for field_name in cls.model_fields:
        if field_name in skip_fields:
            continue
        value = getattr(entity, field_name, None)
        if value is None:
            continue
        # Skip nested Pydantic models
        if hasattr(type(value), "model_fields") and not isinstance(value, str):
            continue
        attr_name = _format_field_name(field_name)
        triples.append(
            (
                URIRef(f"{PURLBASE}/{term}"),
                URIRef(f"{RDFSBASE}#{attr_name}"),
                Literal(str(value)),
            )
        )
    return triples


def _extract_edge_annotations(
    association: Association,
    s_uri: URIRef,
    pred_uri: URIRef,
    o_uri: URIRef,
) -> list[tuple]:
    """Extract edge annotation quintuples from entity fields designated
    in EDGE_ANNOTATION_FIELDS.
    """
    cls_name = type(association).__name__
    mapping = EDGE_ANNOTATION_FIELDS.get(cls_name)
    if not mapping:
        return []
    quintuples = []
    for role in ("subject", "object"):
        fields = mapping.get(role, set())
        if not fields:
            continue
        entity = getattr(association, role, None)
        if entity is None or not hasattr(type(entity), "model_fields"):
            continue
        for field_name in fields:
            value = getattr(entity, field_name, None)
            if value is None:
                continue
            attr_name = _format_field_name(field_name)
            quintuples.append(
                (s_uri, pred_uri, o_uri, URIRef(f"{RDFSBASE}#{attr_name}"), Literal(str(value)))
            )
    return quintuples


def association_to_tuples(
    association: Association,
    context: dict[str, Any] | None = None,
    source: str | None = None,
    annotated_terms: set[str] | None = None,
) -> list[tuple]:
    """Convert an Association instance to a list of RDF tuples.

    Generates the core relationship triple, a source quintuple if
    source is provided, vertex annotation triples for subject/object
    (skipping terms already in annotated_terms), and edge annotation
    quintuples from EDGE_ANNOTATION_FIELDS.

    Parameters
    ----------
    association : Association
        A Pydantic Association subclass instance.
    context : dict, optional
        Context dict with external identifiers (uuid, chembl_id, etc.).
    source : str, optional
        Source label for the source quintuple.
    annotated_terms : set[str], optional
        Set of entity terms that have already been annotated. Terms
        annotated by this call are added to the set. Pass a shared
        set across multiple calls to avoid duplicate vertex annotations.
    """
    ctx = context or {}
    tuples = []

    subj = getattr(association, "subject", None)
    obj = getattr(association, "object", None)
    if subj is None or obj is None:
        return tuples

    s_term = entity_to_term(subj, ctx)
    o_term = entity_to_term(obj, ctx)
    if s_term is None or o_term is None:
        return tuples

    pred_uri = get_predicate_uri(association)
    s_uri = URIRef(f"{PURLBASE}/{s_term}")
    o_uri = URIRef(f"{PURLBASE}/{o_term}")

    # Core relationship triple
    tuples.append((s_uri, pred_uri, o_uri))

    # Source quintuple
    if source:
        tuples.append(
            (s_uri, pred_uri, o_uri, URIRef(f"{RDFSBASE}#Source"), Literal(source))
        )

    # Determine which fields are edge annotations
    cls_name = type(association).__name__
    edge_mapping = EDGE_ANNOTATION_FIELDS.get(cls_name, {})
    subj_edge_fields = edge_mapping.get("subject", set())
    obj_edge_fields = edge_mapping.get("object", set())

    # Vertex annotation triples (skip already-annotated terms)
    if annotated_terms is None or s_term not in annotated_terms:
        tuples.extend(entity_to_annotation_triples(subj, s_term, subj_edge_fields))
        if annotated_terms is not None:
            annotated_terms.add(s_term)

    if annotated_terms is None or o_term not in annotated_terms:
        tuples.extend(entity_to_annotation_triples(obj, o_term, obj_edge_fields))
        if annotated_terms is not None:
            annotated_terms.add(o_term)

    # Edge annotation quintuples from entity fields
    tuples.extend(_extract_edge_annotations(association, s_uri, pred_uri, o_uri))

    return tuples


def write_tuples(tuples: list[tuple], output_path: Path) -> None:
    """Serialize tuples to JSON for ResultsGraphBuilder."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"tuples": tuples}, f, indent=4)
    print(f"Wrote {len(tuples)} tuples to {output_path}")


# ---------------------------------------------------------------------------
# Entity factories
# ---------------------------------------------------------------------------


def make_cell_type(row: pd.Series) -> CellType | None:
    """Create a CellType from a mapping row."""
    cl_id_raw = row.get("cell_ontology_id", "")
    if not cl_id_raw:
        return None
    curie = purl_to_curie(str(cl_id_raw))
    # CellType ontology_purl must match CL:[0-9]{7}
    if not re.match(r"CL:\d{7}$", curie):
        return None
    return CellType(
        ontology_purl=curie,
        label=row.get("cell_ontology_term"),
    )


def make_anatomical_structure(row: pd.Series) -> AnatomicalStructure | None:
    """Create an AnatomicalStructure from a mapping row."""
    uberon_raw = row.get("uberon_entity_id", "")
    if not uberon_raw:
        return None
    curie = purl_to_curie(str(uberon_raw))
    return AnatomicalStructure(
        ontology_purl=curie,
        label=row.get("uberon_entity_term"),
    )


def make_anatomical_structure_from_term(uberon_term: str) -> AnatomicalStructure:
    """Create an AnatomicalStructure from an underscore term like UBERON_0000966."""
    curie = uberon_term.replace("_", ":")
    return AnatomicalStructure(ontology_purl=curie)


def make_cell_set(
    row: pd.Series,
    cell_type: CellType | None = None,
    uberon_curie: str | None = None,
    doi: str | None = None,
    markers: list[str] | None = None,
    binary_genes: list[str] | None = None,
    dataset_name: str | None = None,
    collection_id: str | None = None,
    dataset_version_id: str | None = None,
    assay: str | None = None,
) -> CellSet:
    """Create a CellSet from a merged data row."""
    author_cell_term = hyphenate(str(row.get("clusterName", "")))
    cluster_size = row.get("clusterSize")
    f_score = row.get("f_score")
    silhouette = row.get("median")  # from merged silhouette scores

    return CellSet(
        author_cell_term=author_cell_term,
        assay=assay,
        ontology_purl=cell_type,
        anatomical_structure=uberon_curie,
        species="Homo sapiens",
        publication=doi,
        dataset_name=dataset_name,
        cell_count=int(cluster_size) if pd.notna(cluster_size) else None,
        biomarker_combination=" ".join(markers) if markers else None,
        binary_gene_set=" ".join(binary_genes) if binary_genes else None,
        expressed_genes=" ".join(binary_genes) if binary_genes else None,
        cellxgene_collection=(
            f"cellxgene.cziscience.com/collections/{collection_id}"
            if collection_id
            else None
        ),
        cellxgene_dataset=(
            f"datasets.cellxgene.cziscience.com/{dataset_version_id}.h5ad"
            if dataset_version_id
            else None
        ),
        f_beta_score=float(f_score) if pd.notna(f_score) else None,
        silhouette_score=float(silhouette) if pd.notna(silhouette) else None,
    )


def make_cell_set_dataset(
    dataset_version_id: str,
    summary_data: pd.DataFrame | None = None,
    harvester_row: pd.Series | None = None,
    doi: str | None = None,
    collection_id: str | None = None,
    collection_version_id: str | None = None,
) -> CellSetDataset:
    """Create a CellSetDataset from summary and harvester data."""
    kwargs: dict[str, Any] = {
        "dataset_identifier": dataset_version_id,
        "species": "Homo sapiens",
        "publication": doi,
        "collection_id": collection_id,
        "dataset_collection_version": collection_version_id,
    }

    if summary_data is not None and len(summary_data) > 0:
        s = summary_data.iloc[0]
        kwargs["dataset_name"] = s.get("dataset_title") or s.get("collection_name")
        kwargs["anatomical_structure"] = s.get("organ")
        coll_url = s.get("collection_url")
        if coll_url:
            kwargs["cellxgene_collection"] = str(coll_url)
        ds_url = s.get("explorer_url")
        if ds_url:
            kwargs["cellxgene_dataset"] = str(ds_url)
        n_cells = s.get("n_cells")
        if pd.notna(n_cells):
            kwargs["cell_count"] = int(n_cells)
        journal = s.get("journal")
        first_author = s.get("first_author")
        year = s.get("year")
        if first_author and year:
            kwargs["citation"] = f"{first_author} et al. ({year})"
            if journal:
                kwargs["citation"] += f" {journal}"

    if harvester_row is not None:
        h = harvester_row

        def _hstr(key):
            """Get a string value from harvester row, returning None for NaN."""
            v = h.get(key)
            return str(v) if pd.notna(v) else None

        kwargs.setdefault("dataset_name", _hstr("dataset_title"))
        kwargs.setdefault("anatomical_structure", _hstr("tissue_ontology_term_id"))
        disease = _hstr("disease")
        if disease:
            kwargs["disease_status"] = disease
        total = h.get("total_cell_count")
        if pd.notna(total):
            kwargs.setdefault("cell_count", int(total))
        kwargs.setdefault("cellxgene_collection", _hstr("collection_url"))
        kwargs.setdefault("cellxgene_dataset", _hstr("explorer_url"))
        first_author = _hstr("first_author")
        year = _hstr("year")
        journal = _hstr("journal")
        if first_author and year and "citation" not in kwargs:
            kwargs["citation"] = f"{first_author} et al. ({year})"
            if journal:
                kwargs["citation"] += f" {journal}"

    return CellSetDataset(**kwargs)


def make_publication(row: pd.Series) -> Publication | None:
    """Create a Publication from a mapping row."""
    pmid = row.get("PMID")
    if pd.isna(pmid):
        return None
    return Publication(
        pmid=str(int(pmid)) if isinstance(pmid, float) else str(pmid),
        pmcid=row.get("PMCID"),
        publication_doi=row.get("DOI"),
    )


def make_gene(gene_symbol: str) -> Gene:
    """Create a Gene entity from a gene symbol."""
    return Gene(gene_symbol=gene_symbol)
