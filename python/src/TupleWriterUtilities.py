"""Shared infrastructure for schema-based tuple writers.

Provides constants, helper functions, and generic tuple-generation
functions used by all data-source-specific tuple writer modules.
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
    Drug,
    Gene,
    Mutation,
    Protein,
    Publication,
    linkml_meta as schema_meta,
)

from LoaderUtilities import (
    DATA_DIRPATH,
    PURLBASE,
    RDFSBASE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TUPLES_DIRPATH = DATA_DIRPATH / "tuples"

# Maps Pydantic field names to annotation attribute names where the
# default capitalization convention does not match.
FIELD_NAME_MAP: dict[str, str] = {
    # Example:
    # "f_beta_score": "F_beta_confidence_score",
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
}

# Entity fields that become edge annotation quintuples rather than
# vertex annotation triples.
EDGE_ANNOTATION_FIELDS: dict[str, dict[str, set[str]]] = {
    # Example:
    # "CellSetHasCharacterizingMarkerSetBiomarkerCombination": {
    #     "object": {"f_beta_score"},
    # },
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

    Parameters
    ----------
    purl : str
        An OBO PURL or CURIE string.

    Returns
    -------
    str
        A CURIE (e.g., "CL:0000235"). Returns the input unchanged if
        it does not match the OBO PURL pattern.
    """
    m = re.match(r"https?://purl\.obolibrary\.org/obo/(\w+?)_(\d+)$", purl)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return purl


def parse_string_list(s: str) -> list[str]:
    """Parse a stringified Python list.

    Parameters
    ----------
    s : str
        A string representation of a Python list, e.g.,
        "['SLC12A7', 'OTOGL']".

    Returns
    -------
    list[str]
        The parsed list of strings, or an empty list if parsing fails.
    """
    try:
        result = ast.literal_eval(s)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (ValueError, SyntaxError) as ex:
        print(f"Warning: Could not parse string list: {ex}")
    return []


def curie_to_term(curie: str) -> str:
    """Convert a CURIE to an ArangoDB-compatible underscore term.

    Parameters
    ----------
    curie : str
        A CURIE string (e.g., "CL:0000235").

    Returns
    -------
    str
        The term with colons replaced by underscores (e.g.,
        "CL_0000235").
    """
    return curie.replace(":", "_")


def remove_protocols(value: Any) -> Any:
    """Remove http:// and https:// protocols from a string value.

    Parameters
    ----------
    value : Any
        Any value; only strings are processed.

    Returns
    -------
    Any
        The value with protocols removed if it was a string, otherwise
        unchanged.
    """
    if isinstance(value, str):
        value = value.replace("http://", "")
        value = value.replace("https://", "")
    return value


# ---------------------------------------------------------------------------
# Tuple infrastructure
# ---------------------------------------------------------------------------


def get_predicate_uri(association: Association) -> URIRef:
    """Extract the predicate URI from an Association's linkml_meta.

    Resolves the predicate's ``subproperty_of`` slot name to a full URI
    by looking up ``exact_mappings`` in the schema's slot definitions
    and expanding the CURIE using the schema's prefix definitions.

    Parameters
    ----------
    association : Association
        An Association subclass instance.

    Returns
    -------
    URIRef
        The predicate URI.

    Raises
    ------
    ValueError
        If the predicate's subproperty_of is missing, the slot has no
        exact_mappings, or the CURIE prefix is not defined in the schema.
    """
    cls = type(association)
    meta = cls.model_fields["predicate"].json_schema_extra or {}
    field_meta = meta.get("linkml_meta", {})
    subprop = field_meta.get("subproperty_of")
    if subprop is None:
        raise ValueError(
            f"{cls.__name__} predicate has no subproperty_of in linkml_meta"
        )

    # Look up exact_mappings from schema slot definitions
    slot_meta = schema_meta.root.get("slots", {}).get(subprop)
    if slot_meta is None:
        raise ValueError(
            f"No slot definition for subproperty_of={subprop!r} "
            f"(Association: {cls.__name__})"
        )
    mappings = slot_meta.get("exact_mappings", [])
    curie = mappings[0] if mappings else slot_meta.get("slot_uri")
    if curie is None:
        raise ValueError(
            f"No exact_mappings or slot_uri for slot {subprop!r} "
            f"(Association: {cls.__name__})"
        )

    # Expand CURIE using schema prefix definitions
    prefix, _, local = curie.partition(":")
    prefix_meta = schema_meta.root.get("prefixes", {}).get(prefix)
    if prefix_meta is None:
        raise ValueError(
            f"No prefix definition for {prefix!r} in CURIE {curie!r} "
            f"(Association: {cls.__name__})"
        )
    uri = f"{prefix_meta['prefix_reference']}{local}"
    # OBO PURLs conventionally use http, not https
    uri = uri.replace("https://purl.obolibrary.org/", "http://purl.obolibrary.org/")
    return URIRef(uri)


def entity_to_term(entity: Any, context: dict[str, Any] | None = None) -> str | None:
    """Convert a Pydantic entity instance to an ArangoDB vertex term.

    Parameters
    ----------
    entity : Any
        A Pydantic entity instance (CellType, Gene, Drug, etc.).
    context : dict, optional
        Context dict with external identifiers: ``uuid`` for
        CellSet/BMC/BGS, ``chembl_id`` for Drug.

    Returns
    -------
    str or None
        The ArangoDB vertex term (e.g., "CL_0000235", "GS_TP53"),
        or None if the entity cannot be converted.
    """
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
        dvid = ctx.get("dataset_version_id")
        if dvid:
            return f"PUB_{dvid}"
        doi = remove_protocols(getattr(entity, "publication_doi", None))
        return f"PUB_{doi}" if doi else None

    if isinstance(entity, Drug):
        chembl_id = ctx.get("chembl_id")
        if chembl_id:
            return f"CHEMBL_{chembl_id}"
        drug_label = getattr(entity, "label", None)
        return f"DRUG_{drug_label}" if drug_label else None

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
    """Format a Pydantic field name as an annotation attribute name.

    Uses FIELD_NAME_MAP for special cases, otherwise capitalizes the
    first letter and preserves underscores.

    Parameters
    ----------
    field_name : str
        A Pydantic field name (e.g., "f_beta_score").

    Returns
    -------
    str
        The formatted attribute name (e.g., "F_beta_confidence_score").
    """
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

    Parameters
    ----------
    entity : Any
        A Pydantic entity instance.
    term : str
        The ArangoDB vertex term for this entity.
    edge_fields : set[str], optional
        Field names handled as edge annotations, skipped here.

    Returns
    -------
    list[tuple]
        List of 3-element annotation triples.
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

    Parameters
    ----------
    association : Association
        An Association subclass instance.
    s_uri : URIRef
        Subject URI.
    pred_uri : URIRef
        Predicate URI.
    o_uri : URIRef
        Object URI.

    Returns
    -------
    list[tuple]
        List of 5-element edge annotation quintuples.
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
                (
                    s_uri,
                    pred_uri,
                    o_uri,
                    URIRef(f"{RDFSBASE}#{attr_name}"),
                    Literal(str(value)),
                )
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

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
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

    # Vertex annotation triples (skip already-annotated terms, or allow duplication)
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
    """Serialize tuples to JSON for ResultsGraphBuilder.

    Parameters
    ----------
    tuples : list[tuple]
        List of 3-element and 5-element tuples.
    output_path : Path
        Path to the output JSON file.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"tuples": tuples}, f, indent=4)
    print(f"Wrote {len(tuples)} tuples to {output_path}")


# ---------------------------------------------------------------------------
# Data transformation helpers
# ---------------------------------------------------------------------------


def build_cell_set_dataset(
    dataset_version_id: str,
    summary_data: pd.DataFrame | None = None,
    harvester_row: pd.Series | None = None,
    doi: str | None = None,
    collection_id: str | None = None,
    collection_version_id: str | None = None,
) -> CellSetDataset:
    """Build a CellSetDataset by merging summary and harvester data.

    Extracts and transforms fields from a dataset summary DataFrame
    and/or a CELLxGENE harvester row into a CellSetDataset entity.
    Used by both NSForestTupleWriter and MappingTupleWriter.

    Parameters
    ----------
    dataset_version_id : str
        Dataset version identifier (used as the CSD vertex term).
    summary_data : pd.DataFrame, optional
        DataFrame from dataset summary CSV.
    harvester_row : pd.Series, optional
        Single row from CELLxGENE harvester CSV.
    doi : str, optional
        Publication DOI.
    collection_id : str, optional
        CELLxGENE collection identifier.
    collection_version_id : str, optional
        CELLxGENE collection version identifier.

    Returns
    -------
    tuple[CellSetDataset, str | None]
        The CellSetDataset entity and an optional citation string
        derived from author, year, and journal fields.
    """
    citation = None
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
        first_author = s.get("first_author")
        year = s.get("year")
        journal = s.get("journal")
        if first_author and year:
            citation = f"{first_author} et al. ({year})"
            if journal:
                citation += f" {journal}"

    if harvester_row is not None:
        h = harvester_row

        def _hstr(key):
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
        if first_author and year and citation is None:
            citation = f"{first_author} et al. ({year})"
            if journal:
                citation += f" {journal}"

    return CellSetDataset(**kwargs), citation
