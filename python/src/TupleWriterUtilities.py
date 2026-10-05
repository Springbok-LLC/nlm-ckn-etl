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
    PURLBASE,
    RDFSBASE,
    build_citation,
    build_dataset_label,
    get_current_run,
    map_gene_ensembl_id_to_names,
)

import ProductionDataSpecification as spec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def get_tuples_dir():
    """Return the current run's tuples directory, creating it if needed."""
    tuples_dir = get_current_run().tuples_dir
    tuples_dir.mkdir(parents=True, exist_ok=True)
    return tuples_dir


# Maps Pydantic field names to annotation attribute names where the
# stored name must differ from the Pydantic field name.
FIELD_NAME_MAP: dict[str, str] = {}

# Fields to skip when generating vertex annotation triples because
# their value is already encoded in the vertex term itself.
TERM_ENCODED_FIELDS: dict[str, set[str]] = {
    "CellType": {"ontology_purl"},
    "AnatomicalStructure": {"ontology_purl"},
    "Disease": {"ontology_purl"},
    "BiologicalProcess": {"ontology_purl"},
    "CellularComponent": {"ontology_purl"},
    "MolecularFunction": {"ontology_purl"},
    "Taxon": {"ontology_purl"},
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


def resolve_gene_names(gene_list, ensembl_id_to_names):
    """Replace Ensembl gene ids with gene names where possible.

    Parameters
    ----------
    gene_list : list[str]
        Gene symbols or Ensembl ids.
    ensembl_id_to_names : pd.DataFrame
        Mapping from ``get_gene_ensembl_id_to_names_map()``.

    Returns
    -------
    list[str]
        Gene list with Ensembl ids replaced by names.
    """
    resolved = []
    for gene in gene_list:
        if gene.startswith("ENSG"):
            ensembl_id = gene.split(".")[0]
            names = map_gene_ensembl_id_to_names(ensembl_id, ensembl_id_to_names)
            resolved.append(names[0] if names else gene)
        else:
            resolved.append(gene)
    return resolved


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


def as_float(row, col):
    """Return ``row[col]`` as float, or ``None`` if missing/NaN."""
    v = row.get(col)
    return float(v) if pd.notna(v) else None


def as_int(row, col):
    """Return ``row[col]`` as int, or ``None`` if missing/NaN."""
    v = row.get(col)
    return int(v) if pd.notna(v) else None


def as_str(row, col):
    """Return ``row[col]`` as str, or ``None`` if missing/NaN."""
    v = row.get(col)
    return str(v) if pd.notna(v) else None


def as_count_str(row, col):
    """Return ``row[col]`` as a whole-number string, or ``None``.

    A count column loads as float64 when any row of its file is blank, and
    ``str`` would then render 105445 as ``"105445.0"``.  These counts reach
    the UI as strings, so they go through ``int`` first.
    """
    v = row.get(col)
    return str(int(v)) if pd.notna(v) else None


# Separator the ontology rollups are published with.  The summaries spell
# them with " | " and the harvester with "; ", so whichever source a value
# comes from it is rewritten to one separator, and consumers that split
# these strings need handle only that one (Springbok-LLC/nlm-ckn-etl#63).
ROLLUP_SEPARATOR = " | "
_ROLLUP_SEPARATOR_RE = re.compile(r"\s*[;|]\s*")


def as_rollup_str(row, col):
    """Return ``row[col]`` as a rollup string, or ``None``.

    A rollup is a list of ``"<CURIE>: <count>"`` pairs.  The pairs are
    rejoined on :data:`ROLLUP_SEPARATOR` regardless of how the source
    spelled the separator.
    """
    v = row.get(col)
    if pd.isna(v):
        return None
    return ROLLUP_SEPARATOR.join(
        part for part in _ROLLUP_SEPARATOR_RE.split(str(v).strip()) if part
    )


def as_nonzero_rollup_str(row, col):
    """Return ``row[col]`` as a rollup string with zero counts dropped.

    The summaries pad ``development_stage_summary`` with every stage of the
    HsapDv vocabulary, all but a handful at a count of zero, which would put
    a multi-kilobyte list of stages the dataset does not cover into the
    slot.  A zero-count pair says only that the term was enumerated, so it
    is dropped; a pair whose count cannot be read is kept, since an
    unparsable count is not evidence of absence.
    """
    rollup = as_rollup_str(row, col)
    if rollup is None:
        return None
    kept = []
    for pair in rollup.split(ROLLUP_SEPARATOR):
        # The term itself can contain ": " ("30-year-old stage: 34,167"), so
        # the count is taken from the last separator, and thousands are
        # published with commas.
        _, _, count = pair.rpartition(": ")
        try:
            if int(count.replace(",", "")) == 0:
                continue
        except ValueError:
            pass
        kept.append(pair)
    return ROLLUP_SEPARATOR.join(kept) or None


def as_rollup_terms_str(row, col):
    """Return the terms a rollup in ``row[col]`` names, without their counts.

    A rollup pairs a term with a count ("EFO:0009922: 105445"); this keeps
    the terms alone, so a slot that wants the terms a dataset covers can be
    filled from the same column that fills the slot wanting the counts.
    Zero-count pairs are dropped, as in :func:`as_nonzero_rollup_str`.
    """
    rollup = as_nonzero_rollup_str(row, col)
    if rollup is None:
        return None
    terms = [
        pair.rpartition(": ")[0] or pair for pair in rollup.split(ROLLUP_SEPARATOR)
    ]
    return ROLLUP_SEPARATOR.join(terms) or None


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


def normalize_doi(value: Any) -> str | None:
    """Reduce a publication link to a bare, lowercase DOI.

    Strips the protocol and any DOI resolver host, so that
    "https://doi.org/10.1038/S41591-023-02327-2" and
    "10.1038/s41591-023-02327-2" both yield the latter. DOIs are
    case-insensitive, so the result is lowercased to give a paper one
    identity regardless of how CELLxGENE spelled it.

    Parameters
    ----------
    value : Any
        A DOI, DOI URL, or None; non-string values return None.

    Returns
    -------
    str or None
        The bare DOI, or None if there is none.
    """
    if not isinstance(value, str):
        return None
    doi = remove_protocols(value).strip().lower()
    doi = re.sub(r"^(dx\.)?doi\.org/", "", doi)
    return doi or None


def doi_to_key(value: Any) -> str | None:
    """Convert a DOI to an ArangoDB document key.

    A DOI contains a slash, which cannot appear in a document key, and
    would also break the term parsing in OntologyGraphBuilder, which
    splits the last segment of a vertex PURL on "_" and requires exactly
    two tokens. Characters outside that safe set are replaced with "-",
    so 10.1038/s41591-023-02327-2 becomes 10.1038-s41591-023-02327-2.

    Parameters
    ----------
    value : Any
        A DOI, DOI URL, or None.

    Returns
    -------
    str or None
        The document key, or None if there is no DOI.
    """
    doi = normalize_doi(value)
    if doi is None:
        return None
    key = re.sub(r"[^a-z0-9.-]+", "-", doi)
    key = re.sub(r"-{2,}", "-", key).strip("-")
    return key or None


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
        # Key CellSets by uuid only, like BiomarkerCombination/BinaryGeneSet.
        # The author cell term is retained on the vertex as an attribute, but
        # is kept out of the key because free-text cluster names can contain
        # characters ArangoDB disallows in document keys (e.g. & ï φ), which
        # would otherwise cause the vertex insert to be silently dropped.
        uuid = ctx.get("uuid")
        return f"CS_{uuid}" if uuid else None

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
        # Key Publications by DOI, not by the dataset they were harvested
        # with, so that every dataset of a paper attaches to one shared
        # publication vertex. Datasets whose CELLxGENE record carries no
        # publication link fall back to the dataset version id, which at
        # least keeps them attributed to something.
        key = doi_to_key(getattr(entity, "publication_doi", None))
        if key:
            return f"PUB_{key}"
        dvid = ctx.get("dataset_version_id")
        return f"PUB_{dvid}" if dvid else None

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

    Returns the field name as-is so that vertex attributes match the
    Pydantic schema and ontology conventions. Presentation is handled
    by the front end via the collection maps.

    Uses FIELD_NAME_MAP for special cases where the attribute name
    must differ from the Pydantic field name.

    Parameters
    ----------
    field_name : str
        A Pydantic field name (e.g., "f_beta_score").

    Returns
    -------
    str
        The attribute name (e.g., "f_beta_score").
    """
    if field_name in FIELD_NAME_MAP:
        return FIELD_NAME_MAP[field_name]
    return field_name


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
        Deprecated and ignored. Previously used to suppress duplicate
        vertex annotations by skipping all annotation triples for any
        term already in the set. That caused fields populated by a later
        instance to be silently dropped when an earlier instance of the
        same term was sparser. Vertex-annotation dedup is now performed
        by ``write_tuples`` with last-non-None-wins semantics per
        ``(term, attribute)``. The parameter is retained so existing
        call sites continue to work.

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

    # Vertex annotation triples (emitted unconditionally; dedup with
    # last-non-None-wins semantics happens in write_tuples).
    tuples.extend(entity_to_annotation_triples(subj, s_term, subj_edge_fields))
    tuples.extend(entity_to_annotation_triples(obj, o_term, obj_edge_fields))

    # Edge annotation quintuples from entity fields
    tuples.extend(_extract_edge_annotations(association, s_uri, pred_uri, o_uri))

    return tuples


def _dedupe_annotation_triples_last_wins(tuples: list[tuple]) -> list[tuple]:
    """Collapse duplicate vertex annotation triples to the last occurrence.

    A vertex annotation triple is a 3-element tuple whose predicate URI
    starts with ``RDFSBASE + "#"`` — the scheme used by
    ``entity_to_annotation_triples``. For each ``(subject, predicate)``
    pair only the last such triple in input order is kept, so a later
    non-None value for a given ``(term, attribute)`` wins. Non-annotation
    tuples (core relationship 3-tuples with OBO predicates, and 5-tuples)
    pass through unchanged and preserve their original ordering.
    """
    annotation_prefix = f"{RDFSBASE}#"

    def _is_annotation(t: tuple) -> bool:
        return len(t) == 3 and str(t[1]).startswith(annotation_prefix)

    last_idx: dict[tuple[str, str], int] = {}
    for i, t in enumerate(tuples):
        if _is_annotation(t):
            last_idx[(str(t[0]), str(t[1]))] = i

    out: list[tuple] = []
    for i, t in enumerate(tuples):
        if _is_annotation(t):
            if last_idx[(str(t[0]), str(t[1]))] == i:
                out.append(t)
        else:
            out.append(t)
    return out


def write_tuples(tuples: list[tuple], output_path: Path) -> None:
    """Serialize tuples to JSON for ResultsGraphBuilder.

    Vertex annotation triples are deduplicated with last-non-None-wins
    semantics per ``(term, attribute)`` before serialization.

    Parameters
    ----------
    tuples : list[tuple]
        List of 3-element and 5-element tuples.
    output_path : Path
        Path to the output JSON file.
    """
    tuples = _dedupe_annotation_triples_last_wins(tuples)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"tuples": tuples}, f, indent=4)
    print(f"Wrote {len(tuples)} tuples to {output_path}")


# ---------------------------------------------------------------------------
# Data transformation helpers
# ---------------------------------------------------------------------------


def cell_set_dataset_identifier(dataset_version_id: str, organ: str | None) -> str:
    """Compose the identifier of a cell set dataset from its source dataset
    and the organ it was tissue-filtered for.

    A CELLxGENE ``dataset_version_id`` identifies the unfiltered source h5ad.
    Several NSForest results derive from the same source by filtering it for
    different organs, so keying a cell set dataset on the source id alone
    collapses those filtered datasets into one vertex (Springbok-LLC/nlm-ckn#…).
    The organ makes the identifier unique per filtered dataset, while the
    source id is retained separately (``version``) for CELLxGENE provenance.

    Parameters
    ----------
    dataset_version_id : str
        The source CELLxGENE ``dataset_version_id``.
    organ : str or None
        The organ the source was filtered for, or None when there is no
        organ (an unfiltered dataset), in which case the source id is used
        unchanged so single-organ datasets keep their existing identifier.

    Returns
    -------
    str
        ``"<dataset_version_id>__<organ>"`` normalized, or the bare
        ``dataset_version_id`` when ``organ`` is falsy.
    """
    if not organ or (isinstance(organ, float) and pd.isna(organ)):
        return str(dataset_version_id)
    return f"{dataset_version_id}__{spec.normalize_organ(organ)}"


def build_cell_set_dataset(
    dataset_version_id: str,
    is_reference_dataset: bool,
    summary_data: pd.DataFrame | None = None,
    harvester_row: pd.Series | None = None,
    doi: str | None = None,
    collection_id: str | None = None,
    collection_version_id: str | None = None,
) -> tuple[CellSetDataset, str | None]:
    """Build a CellSetDataset by merging summary and harvester data.

    Extracts and transforms fields from a dataset summary DataFrame
    and/or a CELLxGENE harvester row into a CellSetDataset entity.
    Used by both NSForestTupleWriter and MappingTupleWriter.

    The summary is the primary source and the harvester row the fallback.
    The harvester tables are per-organ and do not cover every dataset the
    pipeline processed, so fields sourced from them alone were absent on
    part of the graph (Springbok-LLC/nlm-ckn-etl#63); the master dataset
    summary carries the same rollups for every dataset.  ``donor_id_count``
    remains harvester-only because no summary reports it.

    Parameters
    ----------
    dataset_version_id : str
        Dataset version identifier (used as the CSD vertex term).
    is_reference_dataset : bool
        Whether this dataset_version_id is backed by a cluster_cid_mapping
        file (see LoaderUtilities.get_reference_dataset_version_ids). No
        default: every call site must decide explicitly, rather than a
        reference dataset silently defaulting to False.
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
    organ = None
    if summary_data is not None and len(summary_data) > 0:
        organ = summary_data.iloc[0].get("organ")
    kwargs: dict[str, Any] = {
        # Identify the filtered dataset by (source, organ); retain the source
        # dataset_version_id in ``version`` for CELLxGENE provenance.
        "dataset_identifier": cell_set_dataset_identifier(dataset_version_id, organ),
        "version": str(dataset_version_id),
        "species": "Homo sapiens",
        "publication": normalize_doi(doi),
        "collection_id": collection_id,
        "dataset_collection_version": collection_version_id,
        "is_reference_dataset": is_reference_dataset,
    }

    def fill(field: str, value: Any) -> None:
        """Set ``field`` only if it has no value yet and ``value`` is one.

        The summary is the primary source and the harvester the fallback,
        so a field the summary left empty can still be filled from the
        harvester row, while one the summary set is never overwritten.
        """
        if value is not None and kwargs.get(field) is None:
            kwargs[field] = value

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
        kwargs["embedding"] = as_str(s, "embedding")
        kwargs["cluster_annotation"] = as_str(s, "cluster_header")
        kwargs["mean_silhouette"] = as_float(s, "mean_silhouette")
        kwargs["standard_deviation_of_silhouette"] = as_float(s, "std_silhouette")
        kwargs["mean_f_beta_score"] = as_float(s, "mean_fscore")
        kwargs["median_of_f_beta_scores"] = as_float(s, "median_fscore")
        kwargs["median_of_median_silhouette"] = as_float(s, "median_silhouette")
        # The summary is the authority for the dataset-scoped counts and
        # ontology rollups: it covers every dataset the pipeline processed,
        # whereas the harvester tables are keyed per organ and miss some
        # (Springbok-LLC/nlm-ckn-etl#63).
        #
        # The slot takes the summary's ``filtered_cell_count``, the count the
        # slot is defined as.  It used to take the harvester's
        # ``normal_cell_count``, a differently-computed quantity, and only the
        # harvester carries that column just as only the summary carries this
        # one (Springbok-LLC/nlm-ckn-etl#64).
        fill("filtered_cell_count", as_count_str(s, "filtered_cell_count"))
        fill("tissue_annotation", as_rollup_str(s, "tissue_ontology_summary"))
        # tissue_annotation_id is deliberately left empty: the summary's
        # tissue_ontology_term_id is the value it wants, but the slot's range
        # TissueEnum is declared with no permissible values, so the schema
        # accepts nothing for it (Springbok-LLC/nlm-ckn-etl#63).
        fill("assay_summary", as_rollup_str(s, "assay_ontology_summary"))
        # ``assay`` names the assay terms the dataset covers and
        # ``assay_summary`` how many cells each contributed, so both come
        # from the one rollup the summary publishes.  Taking the terms from
        # it rather than from the harvester's assay_ontology_term_id keeps
        # the slot filled for every dataset, since the per-organ harvester
        # tables miss some (Springbok-LLC/nlm-ckn-etl#63).
        fill("assay", as_rollup_terms_str(s, "assay_ontology_summary"))
        # Donor ages, as the stages the dataset covers and the cells each
        # contributed.  The summary enumerates the whole HsapDv vocabulary
        # here, so the zero-count stages are dropped.
        fill("donor_age", as_nonzero_rollup_str(s, "development_stage_summary"))
        # cluster_summary is an integer slot as of ckn-schema
        # v0.0.0-alpha.4, so the count goes in as a number rather than
        # the whole-number string the other counts still use.
        fill("cluster_summary", as_int(s, "n_clusters"))
        fill("publication", normalize_doi(as_str(s, "doi")))
        citation = build_citation(
            s.get("first_author"), s.get("year"), s.get("journal")
        )

    if harvester_row is not None:
        h = harvester_row
        fill("dataset_name", as_str(h, "dataset_title"))
        fill("anatomical_structure", as_str(h, "tissue_ontology_term_id"))
        disease = as_str(h, "disease")
        if disease:
            kwargs["disease_status"] = disease
        total = h.get("total_cell_count")
        if pd.notna(total):
            fill("cell_count", int(total))
        fill("cellxgene_collection", as_str(h, "collection_url"))
        fill("cellxgene_dataset", as_str(h, "explorer_url"))
        fill("embedding", as_str(h, "embedding"))
        # No fallback for filtered_cell_count.  The harvester's
        # normal_cell_count is not that count computed elsewhere, it is a
        # different quantity (Springbok-LLC/nlm-ckn-etl#64), so a dataset
        # whose summary omits the count leaves the slot empty rather than
        # carrying a number that does not mean what the slot says.
        fill("assay_summary", as_rollup_str(h, "assay_ontology_summary"))
        # The harvester names the assay terms outright, and its rollup
        # backs that up the way the summary's does.
        fill("assay", as_rollup_str(h, "assay_ontology_term_id"))
        fill("assay", as_rollup_terms_str(h, "assay_ontology_summary"))
        fill("donor_age", as_nonzero_rollup_str(h, "development_stage_summary"))
        fill("tissue_annotation", as_rollup_str(h, "tissue_ontology_summary"))
        fill("dataset_collection_version", as_str(h, "collection_version_id"))
        fill("publication", normalize_doi(as_str(h, "doi")))
        # Donor counts appear only in the harvester tables.
        kwargs["donor_id_count"] = as_int(h, "donor_id_count")
        if citation is None:
            citation = build_citation(
                h.get("first_author"), h.get("year"), h.get("journal")
            )

    return CellSetDataset(**kwargs), citation


def cell_set_dataset_name_tuples(
    term: str, citation: str | None, dataset_name: str | None
) -> list[tuple]:
    """Create the Citation and Name annotations of a cell set dataset.

    A publication can contribute several datasets, and the graph labels a
    cell set dataset by its Name, so the name qualifies the citation with
    the dataset name to tell those datasets apart. Every writer that
    creates a cell set dataset annotates it here, so that all of them
    name it the same way.

    Parameters
    ----------
    term : str
        The cell set dataset vertex term (e.g., "CSD_5774ef6a-4082").
    citation : str or None
        Citation of the publication the dataset was reported in.
    dataset_name : str or None
        Name of the dataset.

    Returns
    -------
    list[tuple]
        The annotation triples, omitting each annotation that has no
        value.
    """
    tuples = []
    for attribute, value in [
        ("Citation", citation),
        ("Name", build_dataset_label(citation, dataset_name)),
    ]:
        if value:
            tuples.append(
                (
                    URIRef(f"{PURLBASE}/{term}"),
                    URIRef(f"{RDFSBASE}#{attribute}"),
                    Literal(value),
                )
            )
    return tuples
