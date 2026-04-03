"""Explore creating Pydantic Association instances from test data and
convert them to RDF tuples for ArangoDB loading.

Systematically attempts to instantiate each Association class from the
available test data's results/data sections (not tuples), reporting
successes, failures, and ambiguities. Maps as many entity fields as
possible from the available data sources. Provides generic functions
to convert any Association instance into the tuple format consumed by
the Java ResultsGraphBuilder.
"""

import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rdflib.term import Literal, URIRef

import ckn_schema.pydantic.ckn_schema as ckn_module
from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    Association,
    BinaryGeneSet,
    BiomarkerCombination,
    BiologicalProcess,
    CellSet,
    CellSetDataset,
    CellType,
    CellularComponent,
    ClinicalTrial,
    Disease,
    Drug,
    Gene,
    LifeCycleStage,
    MolecularFunction,
    Mutation,
    Protein,
    Publication,
    Species,
    VariantConsequence,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_DATA = PROJECT_ROOT / "src" / "test" / "data"
SUMMARIES = TEST_DATA / "summaries"
TUPLES = TEST_DATA / "tuples"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def purl_to_curie(purl: str) -> str:
    """Convert an OBO PURL to a CURIE.

    "http://purl.obolibrary.org/obo/CL_0000235"  -> "CL:0000235"
    "https://purl.obolibrary.org/obo/CL_4030027"  -> "CL:4030027"
    "UBERON:0000955" (already a CURIE)             -> "UBERON:0000955"
    """
    m = re.match(r"https?://purl\.obolibrary\.org/obo/(\w+?)_(\d+)$", purl)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return purl


def parse_string_list(s: str) -> list[str]:
    """Parse a stringified Python list.

    "['SLC12A7', 'OTOGL']" -> ["SLC12A7", "OTOGL"]
    """
    try:
        result = ast.literal_eval(s)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (ValueError, SyntaxError):
        pass
    return []


def get_columnar_row(data: dict, row_key: str = "0") -> dict[str, Any]:
    """Extract a single row from columnar (pandas-style) JSON.

    Columnar format: {"col_name": {"0": value, "1": value, ...}}
    Returns: {"col_name": value, ...} for the given row_key.
    """
    row = {}
    for col, values in data.items():
        if isinstance(values, dict) and row_key in values:
            row[col] = values[row_key]
        elif isinstance(values, dict):
            # Try first available key
            first_key = next(iter(values), None)
            if first_key is not None:
                row[col] = values[first_key]
    return row


def try_create(cls, **kwargs) -> tuple[Any | None, str | None]:
    """Try to create a Pydantic instance, return (instance, error)."""
    try:
        instance = cls(**kwargs)
        return instance, None
    except (ValidationError, Exception) as e:
        return None, str(e)


def extract_uniprot_id_from_link(link: str) -> str | None:
    """Extract UniProt ID from a URL like 'https://www.uniprot.org/uniprot/P19022'."""
    m = re.search(r"/uniprot/([A-Z0-9]+)$", link)
    return m.group(1) if m else None


def curie_to_term(curie: str) -> str:
    """Convert a CURIE like 'CL:0000235' to an ArangoDB term like 'CL_0000235'.

    If already in underscore form, returns as-is.
    """
    return curie.replace(":", "_")


# ---------------------------------------------------------------------------
# Tuple-writing constants
# ---------------------------------------------------------------------------

PURLBASE = "http://purl.obolibrary.org/obo"
RDFSBASE = "http://www.w3.org/1999/02/22-rdf-syntax-ns"

TUPLES_DIRPATH = Path(__file__).parents[2] / "data" / "tuples"

# Maps subproperty_of name (from Association linkml_meta) to OBO URI term.
# Source: exact_mappings in ckn_schema.yaml.
PREDICATE_MAP: dict[str, str] = {
    "part_of": "BFO_0000050",
    "derives_from": "RO_0001000",
    "develops_from": "RO_0002202",
    "composed_primarily_of": "RO_0002473",
    "expresses": "RO_0002292",
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
    "has_pharmacological_effect": "RO_0002027",  # TODO: No exact_mapping in YAML
    "has_plasma_membrane_part": "RO_0002104",
    "lacks_plasma_membrane_part": "CL_4030046",
    "capable_of": "RO_0002215",
    "involved_in": "RO_0002331",
    "located_in": "RO_0001025",
    "exact_match": "SKOS_exactMatch",
    "evaluated_in": "OPMI_0000437",  # TODO: No exact_mapping in YAML
}

# Maps Pydantic field names to the attribute names used in existing tuple
# writers, where the default capitalization convention doesn't match.
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

# Maps (Association class name, "subject"|"object") to sets of entity field
# names that should become edge annotation quintuples rather than vertex
# annotation triples.  Derived from existing NSForest/AuthorToCl tuple
# writer patterns.
#
# The LinkML schema does not distinguish vertex from edge annotations —
# this is an application-level convention.  The existing tuple writers
# place several metrics on the CellSet→BMC edge (F_beta_confidence_score,
# Precision, Recall, TP, TN, FP, FN, Marker_count), but most of those
# come from raw data columns, not from Pydantic entity fields.  Only
# f_beta_score is both a BiomarkerCombination field *and* an edge
# annotation in the existing writers, so it is the only entry here.
EDGE_ANNOTATION_FIELDS: dict[str, dict[str, set[str]]] = {
    "CellSetHasCharacterizingMarkerSetBiomarkerCombination": {
        "object": {"f_beta_score"},
    },
}

# Fields to skip when generating vertex annotation triples, because their
# value is already encoded in the vertex term itself.
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


# ---------------------------------------------------------------------------
# Tuple-writing functions
# ---------------------------------------------------------------------------


def get_predicate_uri(association: Association) -> URIRef:
    """Extract the RO/BFO predicate URI from an Association's linkml_meta.

    Reads the ``subproperty_of`` value from the Association subclass's
    ``predicate`` field metadata and maps it to an OBO URI via
    ``PREDICATE_MAP``.

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
        If the predicate's ``subproperty_of`` is not found in
        ``PREDICATE_MAP``.
    """
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
    # Special cases that use a different base URI
    if subprop == "source":
        return URIRef(f"{RDFSBASE}/dc#Source")
    return URIRef(f"{PURLBASE}/{obo_term}")


def entity_to_term(
    entity: Any, context: dict[str, Any] | None = None
) -> str | None:
    """Convert a Pydantic entity or gene symbol string to an ArangoDB
    vertex term.

    Parameters
    ----------
    entity : Any
        A Pydantic entity instance (CellType, Gene, Drug, etc.) or a
        plain string (gene symbol).
    context : dict, optional
        Context dict with external identifiers: ``uuid`` for
        CellSet/BMC/BGS, ``chembl_id`` for Drug.

    Returns
    -------
    str or None
        The ArangoDB vertex term (e.g., ``"CL_0000235"``), or None if
        the entity cannot be converted.
    """
    ctx = context or {}

    # Plain string — assumed to be a gene symbol
    if isinstance(entity, str):
        return f"GS_{entity}"

    # Entities with ontology_purl
    if isinstance(entity, CellType):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None
    if isinstance(entity, AnatomicalStructure):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None
    if isinstance(entity, Disease):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None
    if isinstance(
        entity, (BiologicalProcess, CellularComponent, MolecularFunction)
    ):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None
    if isinstance(entity, Species):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None
    if isinstance(entity, LifeCycleStage):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None

    # Gene entity
    if isinstance(entity, Gene):
        return f"GS_{entity.gene_symbol}"

    # Protein
    if isinstance(entity, Protein):
        uid = getattr(entity, "uniprot_id", None)
        return f"PR_{uid}" if uid else None

    # CellSet — needs uuid from context
    if isinstance(entity, CellSet):
        name = getattr(entity, "author_cell_term", None)
        uuid = ctx.get("uuid")
        if name and uuid:
            return f"CS_{name}-{uuid}"
        return None

    # CellSetDataset
    if isinstance(entity, CellSetDataset):
        did = getattr(entity, "dataset_identifier", None)
        return f"CSD_{did}" if did else None

    # BiomarkerCombination — needs uuid from context
    if isinstance(entity, BiomarkerCombination):
        uuid = ctx.get("uuid")
        return f"BMC_{uuid}" if uuid else None

    # BinaryGeneSet — needs uuid from context
    if isinstance(entity, BinaryGeneSet):
        uuid = ctx.get("uuid")
        return f"BGS_{uuid}" if uuid else None

    # Publication
    if isinstance(entity, Publication):
        pmid = getattr(entity, "pmid", None)
        return f"PUB_{pmid}" if pmid else None

    # Drug — needs chembl_id from context, falls back to drug_name
    if isinstance(entity, Drug):
        chembl_id = ctx.get("chembl_id")
        if chembl_id:
            return f"CHEMBL_{chembl_id}"
        drug_name = getattr(entity, "drug_name", None)
        return f"DRUG_{drug_name}" if drug_name else None

    # ClinicalTrial
    if isinstance(entity, ClinicalTrial):
        sid = getattr(entity, "study_id", None)
        if sid:
            return sid.replace(":", "_") if ":" in sid else sid
        return None

    # Mutation
    if isinstance(entity, Mutation):
        rsid = getattr(entity, "reference_sequence_identifier", None)
        if rsid:
            # Strip "rs" prefix if present, use RS_ prefix
            num = rsid.lstrip("rs")
            return f"RS_{num}"
        return None

    # VariantConsequence
    if isinstance(entity, VariantConsequence):
        purl = getattr(entity, "ontology_purl", None)
        return curie_to_term(purl) if purl else None

    return None


def _format_field_name(field_name: str) -> str:
    """Format a Pydantic field name as an annotation attribute name.

    Uses ``FIELD_NAME_MAP`` for special cases, otherwise capitalizes
    the first letter and preserves underscores.
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

    For each non-None field, produces a triple:
    ``(entity_URI, attribute_URI, Literal(value))``

    Parameters
    ----------
    entity : Any
        A Pydantic entity instance.
    term : str
        The ArangoDB vertex term for this entity.
    edge_fields : set[str], optional
        Field names that are handled as edge annotations — these are
        skipped here to avoid duplicating them as vertex annotations.

    Returns
    -------
    list[tuple]
        List of 3-element annotation triples.
    """
    if isinstance(entity, str):
        return []

    triples = []
    cls_name = type(entity).__name__
    skip_fields = TERM_ENCODED_FIELDS.get(cls_name, set())
    if edge_fields:
        skip_fields = skip_fields | edge_fields

    for field_name in entity.model_fields:
        if field_name in skip_fields:
            continue
        value = getattr(entity, field_name, None)
        if value is None:
            continue
        # Skip nested Pydantic models (e.g., CellSet.ontology_purl is CellType)
        if hasattr(value, "model_fields"):
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
    in ``EDGE_ANNOTATION_FIELDS``.

    Returns 5-element tuples for each matching field that has a non-None
    value on the subject or object entity.
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
        if entity is None or isinstance(entity, str):
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
) -> list[tuple]:
    """Convert an Association instance to a list of RDF tuples.

    Generates:
    - The core relationship triple (3-element)
    - A source quintuple (5-element) if ``source`` is provided
    - Vertex annotation triples for populated fields on subject/object
    - Edge annotation quintuples derived from entity fields listed in
      ``EDGE_ANNOTATION_FIELDS``

    Parameters
    ----------
    association : Association
        A Pydantic Association subclass instance with subject, predicate,
        and object populated.
    context : dict, optional
        Context dict with: ``uuid`` (for CellSet/BMC/BGS terms),
        ``chembl_id`` (for Drug terms).
    source : str, optional
        Source label (e.g., "NSForest", "Manual Mapping") for the source
        quintuple.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element tuples.
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

    # Determine which fields are edge annotations (to exclude from vertex)
    cls_name = type(association).__name__
    edge_mapping = EDGE_ANNOTATION_FIELDS.get(cls_name, {})
    subj_edge_fields = edge_mapping.get("subject", set())
    obj_edge_fields = edge_mapping.get("object", set())

    # Vertex annotation triples for subject and object
    tuples.extend(entity_to_annotation_triples(subj, s_term, subj_edge_fields))
    tuples.extend(entity_to_annotation_triples(obj, o_term, obj_edge_fields))

    # Edge annotation quintuples derived from entity fields
    tuples.extend(_extract_edge_annotations(association, s_uri, pred_uri, o_uri))

    return tuples


def collect_tuples(
    results_dict: dict[str, dict],
) -> list[tuple]:
    """Iterate over all created Association instances and collect tuples.

    Parameters
    ----------
    results_dict : dict
        The ``results`` dict populated by ``record()``, keyed by
        Association class name. Each value has ``"instance"`` and
        ``"source"`` keys.

    Returns
    -------
    list[tuple]
        All tuples collected from successfully created Associations.
    """
    all_tuples = []
    for name, r in results_dict.items():
        if r.get("status") != "Created" or r.get("instance") is None:
            continue
        instance = r["instance"]
        source_label = r.get("source", "")
        tuples = association_to_tuples(
            instance,
            context=r.get("context"),
            source=source_label,
        )
        all_tuples.extend(tuples)
    return all_tuples


def write_tuples(tuples: list[tuple], output_path: Path) -> None:
    """Serialize tuples to JSON in the format expected by
    ResultsGraphBuilder.

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
# Load all data sources once
# ---------------------------------------------------------------------------

def load_all_data() -> dict[str, Any]:
    """Load and return all test data sources."""
    data = {}

    with open(SUMMARIES / "hubmap-allen-brain-v1.7.json") as f:
        data["hubmap"] = json.load(f)

    with open(SUMMARIES / "nlm-ckn-nsforest-results-li-2023.json") as f:
        data["nsforest"] = json.load(f)

    with open(SUMMARIES / "nlm-ckn-map-author-to-cl-li-2023.json") as f:
        data["author_to_cl"] = json.load(f)

    with open(SUMMARIES / "nlm-ckn-external-api-results.json") as f:
        data["external_api"] = json.load(f)

    return data


# ---------------------------------------------------------------------------
# Extract tuples metadata (author, title, year, journal from tuples section)
# ---------------------------------------------------------------------------

def extract_publication_from_tuples(tuples: list) -> dict[str, str]:
    """Extract publication metadata from RDF tuples."""
    pub = {}
    for t in tuples:
        if len(t) >= 3:
            pred = t[1]
            obj = t[2]
            if pred.endswith("#Author"):
                pub["author_list"] = obj
            elif pred.endswith("#Title"):
                pub["title"] = obj
            elif pred.endswith("#Year"):
                pub["year"] = obj
            elif pred.endswith("#Journal"):
                pub["journal"] = obj
    return pub


# ---------------------------------------------------------------------------
# Auto-discover Association subclasses from the schema module
# ---------------------------------------------------------------------------

ASSOCIATION_CLASSES: dict[str, type[Association]] = {
    name: cls
    for name, cls in inspect.getmembers(ckn_module, inspect.isclass)
    if issubclass(cls, Association) and cls is not Association
}

ALL_ASSOCIATIONS = sorted(ASSOCIATION_CLASSES.keys())

results: dict[str, dict] = {}


def record(name: str, status: str, source: str, instance: Any = None,
           error: str | None = None, notes: str = "",
           context: dict[str, Any] | None = None) -> None:
    """Record a result for a given Association class."""
    results[name] = {
        "status": status,
        "source": source,
        "instance": instance,
        "error": error,
        "notes": notes,
        "context": context,
    }


# ---------------------------------------------------------------------------
# 1. hubmap-allen-brain-v1.7.json (data section)
# ---------------------------------------------------------------------------

def from_hubmap(data: dict) -> None:
    """Extract associations from HuBMAP data section."""
    hubmap = data["hubmap"]["data"]["hubmap"]
    cell_types = hubmap.get("cell_types", [])
    anat_structs = hubmap.get("anatomical_structures", [])

    # --- CellTypePartOfAnatomicalStructure from ccf_located_in ---
    for ct in cell_types:
        for uberon_id in ct.get("ccf_located_in", []):
            inst, err = try_create(
                ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"],
                subject=CellType(
                    ontology_purl=ct["id"],
                    label=ct.get("ccf_pref_label"),
                ),
                predicate="part_of",
                object=AnatomicalStructure(ontology_purl=uberon_id),
            )
            record("CellTypePartOfAnatomicalStructure",
                   "Created" if inst else "FAILED",
                   "hubmap", inst, err,
                   f"{ct['id']} located_in {uberon_id}. "
                   "Note: ccf_located_in vs part_of semantic mismatch")
            break
        break

    # --- AnatomicalStructurePartOfAnatomicalStructure from ccf_part_of ---
    for as_ in anat_structs:
        for parent_id in as_.get("ccf_part_of", []):
            inst, err = try_create(
                ASSOCIATION_CLASSES["AnatomicalStructurePartOfAnatomicalStructure"],
                subject=AnatomicalStructure(
                    ontology_purl=as_["id"],
                    label=as_.get("ccf_pref_label"),
                ),
                predicate="part_of",
                object=AnatomicalStructure(ontology_purl=parent_id),
            )
            record("AnatomicalStructurePartOfAnatomicalStructure",
                   "Created" if inst else "FAILED",
                   "hubmap", inst, err,
                   f"{as_['id']} part_of {parent_id}")
            break
        break


# ---------------------------------------------------------------------------
# 2. nlm-ckn-nsforest-results-li-2023.json (results section)
# ---------------------------------------------------------------------------

def from_nsforest(data: dict) -> None:
    """Extract associations from NSForest results."""
    nsforest = data["nsforest"]
    row = get_columnar_row(nsforest.get("results", {}))
    cluster_name = row.get("clusterName", "")
    cluster_size = row.get("clusterSize")
    f_score = row.get("f_score")
    uuid = row.get("uuid", "")
    markers_str = row.get("NSForest_markers", "")
    binary_str = row.get("binary_genes", "")

    markers = parse_string_list(markers_str)
    binary_genes = parse_string_list(binary_str)

    # Build entities with all available fields
    bmc = BiomarkerCombination(
        markers=" ".join(markers),
        f_beta_score=f_score,
    )
    bgs = BinaryGeneSet(markers=" ".join(binary_genes))
    cell_set = CellSet(
        author_cell_term=cluster_name,
        cell_count=int(cluster_size) if cluster_size else None,
        biomarker_combination=" ".join(markers),
        binary_gene_set=" ".join(binary_genes),
    )

    # --- GenePartOfBiomarkerCombination ---
    if markers:
        gene_symbol = markers[0]
        inst, err = try_create(
            ASSOCIATION_CLASSES["GenePartOfBiomarkerCombination"],
            subject=gene_symbol,
            predicate="part_of",
            object=bmc,
        )
        record("GenePartOfBiomarkerCombination",
               "Created" if inst else "FAILED",
               "nsforest", inst, err,
               f"Gene {gene_symbol} part_of BMC({markers})")

    # --- CellSetHasCharacterizingMarkerSetBiomarkerCombination ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellSetHasCharacterizingMarkerSetBiomarkerCombination"],
        subject=cell_set,
        predicate="has_characterizing_marker_set",
        object=bmc,
    )
    record("CellSetHasCharacterizingMarkerSetBiomarkerCombination",
           "Created" if inst else "FAILED",
           "nsforest", inst, err,
           f"CellSet({cluster_name}) -> BMC({markers})")

    # --- BiomarkerCombinationSubclusterOfBinaryGeneSet ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["BiomarkerCombinationSubclusterOfBinaryGeneSet"],
        subject=bmc,
        predicate="subcluster_of",
        object=bgs,
    )
    record("BiomarkerCombinationSubclusterOfBinaryGeneSet",
           "Created" if inst else "FAILED",
           "nsforest", inst, err,
           f"BMC({markers}) subcluster_of BGS({len(binary_genes)} genes)")


# ---------------------------------------------------------------------------
# 3. nlm-ckn-map-author-to-cl-li-2023.json (results + tuples sections)
# ---------------------------------------------------------------------------

def from_author_to_cl(data: dict) -> None:
    """Extract associations from author-to-CL mapping results."""
    author_to_cl = data["author_to_cl"]
    row = get_columnar_row(author_to_cl.get("results", {}))
    tuples = author_to_cl.get("tuples", [])

    # Extract publication metadata from tuples section
    pub_meta = extract_publication_from_tuples(tuples)

    cl_purl = row.get("cell_ontology_id", "")
    cl_curie = purl_to_curie(cl_purl)
    uberon_purl = row.get("uberon_entity_id", "")
    uberon_curie = purl_to_curie(uberon_purl)
    author_cell_term = row.get("author_cell_term", "")
    cluster_name = row.get("clusterName", "")
    cluster_size = row.get("clusterSize")
    uuid = row.get("uuid", "")
    dataset_version_id = row.get("dataset_version_id", "")
    dataset_id = row.get("dataset_id", "")
    collection_id = row.get("collection_id", "")
    collection_version_id = row.get("collection_version_id", "")
    pmid = str(row.get("PMID", ""))
    pmcid = row.get("PMCID", "")
    doi = row.get("DOI", "")
    dataset_source = row.get("dataset_source", "")
    mapping_method = row.get("mapping_method", "")
    markers_str = row.get("NSForest_markers", "")
    binary_str = row.get("binary_genes", "")
    markers = parse_string_list(markers_str)
    binary_genes = parse_string_list(binary_str)

    # Build entities with all available fields populated
    cell_type = CellType(
        ontology_purl=cl_curie,
        label=row.get("cell_ontology_term"),
    )
    anat_struct = AnatomicalStructure(
        ontology_purl=uberon_curie,
        label=row.get("uberon_entity_term"),
    )
    cell_set = CellSet(
        author_cell_term=author_cell_term,
        cell_count=int(cluster_size) if cluster_size else None,
        ontology_purl=cell_type,
        anatomical_structure=uberon_curie,
        species="Homo sapiens",
        publication=doi,
        dataset_name="snRNA-seq of human retina - all cells",
        biomarker_combination=" ".join(markers),
        binary_gene_set=" ".join(binary_genes),
        expressed_genes=" ".join(binary_genes),
        cellxgene_collection=(
            f"cellxgene.cziscience.com/collections/{collection_id}"
        ),
        cellxgene_dataset=(
            f"datasets.cellxgene.cziscience.com/{dataset_version_id}.h5ad"
        ),
    )
    dataset = CellSetDataset(
        dataset_name="snRNA-seq of human retina - all cells",
        dataset_identifier=dataset_version_id,
        species="Homo sapiens",
        dataset_collection_version=collection_version_id,
        publication=doi,
        anatomical_structure=row.get("uberon_entity_term"),
        disease_status="normal",
        cell_type=row.get("cell_ontology_term"),
        cellxgene_collection=(
            f"cellxgene.cziscience.com/collections/{collection_id}"
        ),
    )
    publication = Publication(
        pmid=pmid,
        pmcid=pmcid,
        publication_doi=doi,
        title=pub_meta.get("title"),
        author_list=pub_meta.get("author_list"),
        year=pub_meta.get("year"),
        journal=pub_meta.get("journal"),
    )
    bmc = BiomarkerCombination(markers=" ".join(markers))
    bgs = BinaryGeneSet(markers=" ".join(binary_genes))

    # --- CellTypePartOfAnatomicalStructure ---
    if "CellTypePartOfAnatomicalStructure" not in results:
        inst, err = try_create(
            ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"],
            subject=cell_type,
            predicate="part_of",
            object=anat_struct,
        )
        record("CellTypePartOfAnatomicalStructure",
               "Created" if inst else "FAILED",
               "author-to-cl", inst, err,
               f"{cl_curie} part_of {uberon_curie}")

    # --- CellSetComposedPrimarilyOfCellType ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellSetComposedPrimarilyOfCellType"],
        subject=cell_set,
        predicate="composed_primarily_of",
        object=cell_type,
    )
    record("CellSetComposedPrimarilyOfCellType",
           "Created" if inst else "FAILED",
           "author-to-cl", inst, err,
           f"CellSet({author_cell_term}) -> CellType({cl_curie})")

    # --- CellSetDerivesFromAnatomicalStructure ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellSetDerivesFromAnatomicalStructure"],
        subject=cell_set,
        predicate="derives_from",
        object=anat_struct,
    )
    record("CellSetDerivesFromAnatomicalStructure",
           "Created" if inst else "FAILED",
           "author-to-cl", inst, err,
           f"CellSet({author_cell_term}) derives_from {uberon_curie}")

    # --- CellSetHasSourceCellSetDataset ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellSetHasSourceCellSetDataset"],
        subject=cell_set,
        predicate="source",
        object=dataset,
    )
    record("CellSetHasSourceCellSetDataset",
           "Created" if inst else "FAILED",
           "author-to-cl", inst, err,
           f"CellSet -> CellSetDataset({dataset_version_id[:12]}...)")

    # --- CellSetDatasetHasSourcePublication ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellSetDatasetHasSourcePublication"],
        subject=dataset,
        predicate="source",
        object=publication,
    )
    record("CellSetDatasetHasSourcePublication",
           "Created" if inst else "FAILED",
           "author-to-cl", inst, err,
           f"CellSetDataset -> Publication(PMID:{pmid}). "
           "Publication fields populated from both results and tuples")

    # --- CellTypeHasExemplarDataCellSetDataset ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellTypeHasExemplarDataCellSetDataset"],
        subject=cell_type,
        predicate="has_exemplar_data",
        object=dataset,
    )
    record("CellTypeHasExemplarDataCellSetDataset",
           "Created" if inst else "FAILED",
           "author-to-cl", inst, err,
           f"CellType({cl_curie}) -> CellSetDataset")

    # --- CellTypeExpressesGene ---
    if markers:
        inst, err = try_create(
            ASSOCIATION_CLASSES["CellTypeExpressesGene"],
            subject=cell_type,
            predicate="expresses",
            object=markers[0],
        )
        record("CellTypeExpressesGene",
               "Created" if inst else "FAILED",
               "author-to-cl", inst, err,
               f"CellType({cl_curie}) expresses {markers[0]}. "
               "Note: object is str (gene symbol), not Gene entity")

    # --- CellSetExpressesBinaryGeneSet ---
    inst, err = try_create(
        ASSOCIATION_CLASSES["CellSetExpressesBinaryGeneSet"],
        subject=cell_set,
        predicate="expresses",
        object=bgs,
    )
    record("CellSetExpressesBinaryGeneSet",
           "Created" if inst else "FAILED",
           "author-to-cl", inst, err,
           f"CellSet({author_cell_term}) -> BGS({len(binary_genes)} genes)")

    # --- Repeat BMC/BGS associations if not already recorded ---
    if "GenePartOfBiomarkerCombination" not in results and markers:
        inst, err = try_create(
            ASSOCIATION_CLASSES["GenePartOfBiomarkerCombination"],
            subject=markers[0],
            predicate="part_of",
            object=bmc,
        )
        record("GenePartOfBiomarkerCombination",
               "Created" if inst else "FAILED",
               "author-to-cl", inst, err,
               f"Gene({markers[0]}) part_of BMC")

    if "CellSetHasCharacterizingMarkerSetBiomarkerCombination" not in results:
        inst, err = try_create(
            ASSOCIATION_CLASSES["CellSetHasCharacterizingMarkerSetBiomarkerCombination"],
            subject=cell_set,
            predicate="has_characterizing_marker_set",
            object=bmc,
        )
        record("CellSetHasCharacterizingMarkerSetBiomarkerCombination",
               "Created" if inst else "FAILED",
               "author-to-cl", inst, err)

    if "BiomarkerCombinationSubclusterOfBinaryGeneSet" not in results:
        inst, err = try_create(
            ASSOCIATION_CLASSES["BiomarkerCombinationSubclusterOfBinaryGeneSet"],
            subject=bmc,
            predicate="subcluster_of",
            object=bgs,
        )
        record("BiomarkerCombinationSubclusterOfBinaryGeneSet",
               "Created" if inst else "FAILED",
               "author-to-cl", inst, err)


# ---------------------------------------------------------------------------
# 4. nlm-ckn-external-api-results.json (results section)
# ---------------------------------------------------------------------------

def from_external_api(data: dict) -> None:
    """Extract associations from external API results."""
    api_data = data["external_api"]
    api_results = api_data.get("results", {})
    api_tuples = api_data.get("tuples", {})

    # --- CELLxGENE data for enriching CellSetDataset ---
    cellxgene = api_results.get("cellxgene", {})
    cxg_record = None
    for cxg_id, cxg_data in cellxgene.items():
        if isinstance(cxg_data, dict):
            cxg_record = cxg_data
            break

    # --- Gene data from NCBI Gene ---
    gene_section = api_results.get("gene", {})
    gene_entrez_ids = gene_section.get("gene_entrez_ids", [])
    gene_data = None
    if gene_entrez_ids:
        gene_data = gene_section.get(gene_entrez_ids[0])
        if isinstance(gene_data, dict):
            pass  # gene_data is the dict we want
        else:
            gene_data = None

    # --- UniProt data ---
    uniprot_section = api_results.get("uniprot", {})
    protein_accessions = uniprot_section.get("protein_accessions", [])
    uniprot_data = None
    if protein_accessions:
        uniprot_data = uniprot_section.get(protein_accessions[0])
        if isinstance(uniprot_data, dict):
            pass
        else:
            uniprot_data = None

    # --- OpenTargets ---
    ot = api_results.get("opentargets", {})
    gene_ids = ot.get("gene_ensembl_ids", [])
    if not gene_ids:
        return
    gene_id = gene_ids[0]
    ot_data = ot.get(gene_id, {})

    target = ot_data.get("target", {})
    diseases = ot_data.get("diseases", [])
    drugs = ot_data.get("drugs", [])
    interactions = ot_data.get("interactions", [])
    pharmacogenetics = ot_data.get("pharmacogenetics", [])

    gene_symbol = target.get("approvedSymbol", "")

    # Find UniProt SwissProt ID from target
    protein_ids = target.get("proteinIds", [])
    uniprot_id = None
    for pid in protein_ids:
        if pid.get("source") == "uniprot_swissprot":
            uniprot_id = pid["id"]
            break

    # Build a fully-populated Protein from both OpenTargets and UniProt data
    protein_kwargs: dict[str, Any] = {"gene_symbol": gene_symbol}
    if uniprot_id:
        protein_kwargs["uniprot_id"] = uniprot_id
    if uniprot_data:
        protein_kwargs.setdefault("uniprot_id", uniprot_data.get("UniProt_ID"))
        protein_kwargs["label"] = uniprot_data.get("Protein_name")
        protein_kwargs["number_of_amino_acids"] = uniprot_data.get(
            "Number_of_amino_acids"
        )
        protein_kwargs["protein_function"] = uniprot_data.get("Function")
        protein_kwargs["species"] = uniprot_data.get("Organism")
        protein_kwargs["gene_symbol"] = uniprot_data.get("Gene_name")
        ann_score = uniprot_data.get("Annotation_score")
        if ann_score is not None:
            protein_kwargs["annotation_score"] = int(ann_score)

    # --- GeneGeneticallyInteractsWithGene ---
    if interactions:
        ix = interactions[0]
        target_b = ix.get("targetB", {})
        gene_b_symbol = target_b.get("approvedSymbol", "")
        inst, err = try_create(
            ASSOCIATION_CLASSES["GeneGeneticallyInteractsWithGene"],
            subject=gene_symbol,
            predicate="genetically_interacts_with",
            object=gene_b_symbol,
        )
        record("GeneGeneticallyInteractsWithGene",
               "Created" if inst else "FAILED",
               "external-api (opentargets)", inst, err,
               f"{gene_symbol} <-> {gene_b_symbol}")

    # --- GeneHasQualityMutation ---
    if pharmacogenetics:
        pg = pharmacogenetics[0]
        variantRsId = pg["variantRsId"]
        if variantRsId:
            inst, err = try_create(
                ASSOCIATION_CLASSES["GeneHasQualityMutation"],
                subject=gene_symbol,
                predicate="has_quality",
                object=Mutation(reference_sequence_identifier=variantRsId),
            )
            record("GeneHasQualityMutation",
                   "Created" if inst else "FAILED",
                   "external-api (opentargets)", inst, err,
                   f"{gene_symbol} <-> {variantRsId}")

    # --- GeneIsGeneticBasisForDisease ---
    if diseases:
        d = diseases[0]
        disease_info = d.get("disease", {})
        disease_id = disease_info.get("id", "")
        # Convert MONDO_0009061 to MONDO:0009061
        disease_curie = disease_id.replace("_", ":")
        # Collect dbXRefs as database_cross_reference
        db_xrefs = disease_info.get("dbXRefs", [])
        db_xref_str = ", ".join(db_xrefs) if db_xrefs else None
        inst, err = try_create(
            ASSOCIATION_CLASSES["GeneIsGeneticBasisForDisease"],
            subject=gene_symbol,
            predicate="is_genetic_basis_for_condition",
            object=Disease(
                ontology_purl=disease_curie,
                label=disease_info.get("name"),
                definition=disease_info.get("description"),
                database_cross_reference=db_xref_str,
            ),
        )
        record("GeneIsGeneticBasisForDisease",
               "Created" if inst else "FAILED",
               "external-api (opentargets)", inst, err,
               f"{gene_symbol} -> Disease({disease_curie})")

    # --- GeneMolecularlyInteractsWithDrug ---
    if drugs:
        drug_entry = drugs[0]
        drug_info = drug_entry.get("drug", {})
        drug_name = drug_info.get("name", drug_entry.get("approvedName", ""))
        trade_names = drug_info.get("tradeNames", [])
        synonyms = drug_info.get("synonyms", [])
        is_approved = drug_info.get("isApproved", False)
        inst, err = try_create(
            ASSOCIATION_CLASSES["GeneMolecularlyInteractsWithDrug"],
            subject=gene_symbol,
            predicate="molecularly_interacts_with",
            object=Drug(
                drug_name=drug_name,
                mechanism_of_action=drug_entry.get("mechanismOfAction"),
                trade_names=", ".join(trade_names) if trade_names else None,
                exact_synonym=", ".join(synonyms) if synonyms else None,
                approval_status="approved" if is_approved else "not approved",
                uniprot_id=uniprot_id,
                protein=gene_symbol,
            ),
        )
        record("GeneMolecularlyInteractsWithDrug",
               "Created" if inst else "FAILED",
               "external-api (opentargets)", inst, err,
               f"Gene({gene_symbol}) -> Drug({drug_name})")

    # --- GeneProducesProtein ---
    if protein_kwargs.get("uniprot_id"):
        protein = Protein(**protein_kwargs)
        inst, err = try_create(
            ASSOCIATION_CLASSES["GeneProducesProtein"],
            subject=gene_symbol,
            predicate="produces",
            object=protein,
        )
        record("GeneProducesProtein",
               "Created" if inst else "FAILED",
               "external-api (opentargets+uniprot)", inst, err,
               f"{gene_symbol} -> Protein({protein_kwargs.get('uniprot_id')}). "
               f"Protein fields from UniProt: label, function, amino_acids, "
               f"annotation_score, species, gene_symbol")
    else:
        record("GeneProducesProtein", "PARTIAL",
               "external-api (opentargets)", None, None,
               "No UniProt ID found in target proteinIds")

    # --- DrugIsSubstanceThatTreatsDisease ---
    if drugs:
        drug_entry = drugs[0]
        drug_info = drug_entry.get("drug", {})
        drug_name = drug_info.get("name", drug_entry.get("approvedName", ""))
        trade_names = drug_info.get("tradeNames", [])
        synonyms = drug_info.get("synonyms", [])
        is_approved = drug_info.get("isApproved", False)
        disease_id = drug_entry.get("diseaseId", "")
        disease_curie = disease_id.replace("_", ":")
        # Look up indication details from the drug's indications
        indications = drug_info.get("indications", {}).get("rows", [])
        disease_label = None
        disease_desc = None
        for ind in indications:
            ind_disease = ind.get("disease", {})
            if ind_disease.get("id", "").replace("_", ":") == disease_curie:
                disease_label = ind_disease.get("name")
                disease_desc = ind_disease.get("description")
                break
        inst, err = try_create(
            ASSOCIATION_CLASSES["DrugIsSubstanceThatTreatsDisease"],
            subject=Drug(
                drug_name=drug_name,
                mechanism_of_action=drug_entry.get("mechanismOfAction"),
                trade_names=", ".join(trade_names) if trade_names else None,
                exact_synonym=", ".join(synonyms) if synonyms else None,
                approval_status="approved" if is_approved else "not approved",
                disease=disease_curie,
                uniprot_id=uniprot_id,
                protein=gene_symbol,
            ),
            predicate="is_substance_that_treats",
            object=Disease(
                ontology_purl=disease_curie,
                label=disease_label,
                definition=disease_desc,
            ),
        )
        record("DrugIsSubstanceThatTreatsDisease",
               "Created" if inst else "FAILED",
               "external-api (opentargets)", inst, err,
               f"Drug({drug_name}) -> Disease({disease_curie}). "
               f"Note: diseaseId uses EFO namespace, not MONDO")

    # --- DrugEvaluatedInClinicalTrial ---
    ct_created = False
    for drug_entry in drugs:
        ct_ids = drug_entry.get("ctIds", [])
        if ct_ids:
            drug_info = drug_entry.get("drug", {})
            drug_name = drug_info.get("name", drug_entry.get("approvedName", ""))
            trade_names = drug_info.get("tradeNames", [])
            synonyms = drug_info.get("synonyms", [])
            is_approved = drug_info.get("isApproved", False)
            ct_id = ct_ids[0]
            inst, err = try_create(
                ASSOCIATION_CLASSES["DrugEvaluatedInClinicalTrial"],
                subject=Drug(
                    drug_name=drug_name,
                    mechanism_of_action=drug_entry.get("mechanismOfAction"),
                    trade_names=(
                        ", ".join(trade_names) if trade_names else None
                    ),
                    exact_synonym=(
                        ", ".join(synonyms) if synonyms else None
                    ),
                    approval_status=(
                        "approved" if is_approved else "not approved"
                    ),
                    study_id=ct_id,
                ),
                predicate="evaluated_in",
                object=ClinicalTrial(study_id=ct_id),
            )
            record("DrugEvaluatedInClinicalTrial",
                   "Created" if inst else "FAILED",
                   "external-api (opentargets)", inst, err,
                   f"Drug({drug_name}) -> ClinicalTrial({ct_id}). "
                   "Note: only some drugs have ctIds")
            ct_created = True
            break
    if not ct_created:
        record("DrugEvaluatedInClinicalTrial", "PARTIAL",
               "external-api (opentargets)", None, None,
               "No drugs in test data have ctIds")

    # --- DrugMolecularlyInteractsWithGene ---
    if drugs:
        drug_entry = drugs[0]
        drug_info = drug_entry.get("drug", {})
        drug_name = drug_info.get("name", drug_entry.get("approvedName", ""))
        trade_names = drug_info.get("tradeNames", [])
        synonyms = drug_info.get("synonyms", [])
        is_approved = drug_info.get("isApproved", False)
        inst, err = try_create(
            ASSOCIATION_CLASSES["DrugMolecularlyInteractsWithGene"],
            subject=Drug(
                drug_name=drug_name,
                mechanism_of_action=drug_entry.get("mechanismOfAction"),
                trade_names=", ".join(trade_names) if trade_names else None,
                exact_synonym=", ".join(synonyms) if synonyms else None,
                approval_status="approved" if is_approved else "not approved",
                uniprot_id=uniprot_id,
                protein=gene_symbol,
            ),
            predicate="molecularly_interacts_with",
            object=gene_symbol,
        )
        record("DrugMolecularlyInteractsWithGene",
               "Created" if inst else "FAILED",
               "external-api (opentargets)", inst, err,
               f"Drug({drug_name}) -> Gene({gene_symbol})")

    # --- DrugMolecularlyInteractsWithProtein ---
    if drugs and protein_kwargs.get("uniprot_id"):
        drug_entry = drugs[0]
        drug_info = drug_entry.get("drug", {})
        drug_name = drug_info.get("name", drug_entry.get("approvedName", ""))
        trade_names = drug_info.get("tradeNames", [])
        synonyms = drug_info.get("synonyms", [])
        is_approved = drug_info.get("isApproved", False)
        protein = Protein(**protein_kwargs)
        inst, err = try_create(
            ASSOCIATION_CLASSES["DrugMolecularlyInteractsWithProtein"],
            subject=Drug(
                drug_name=drug_name,
                mechanism_of_action=drug_entry.get("mechanismOfAction"),
                trade_names=", ".join(trade_names) if trade_names else None,
                exact_synonym=", ".join(synonyms) if synonyms else None,
                approval_status="approved" if is_approved else "not approved",
                uniprot_id=uniprot_id,
                protein=gene_symbol,
            ),
            predicate="molecularly_interacts_with",
            object=protein,
        )
        record("DrugMolecularlyInteractsWithProtein",
               "Created" if inst else "FAILED",
               "external-api (opentargets+uniprot)", inst, err,
               f"Drug({drug_name}) -> Protein({uniprot_id}). "
               "Note: indirect - drug targets gene, gene produces protein")
    elif not drugs:
        record("DrugMolecularlyInteractsWithProtein", "PARTIAL",
               "external-api (opentargets)", None, None,
               "No drugs in test data")

    # --- MutationHasPharamcologicalEffectDrug ---
    if pharmacogenetics:
        pg = pharmacogenetics[0]
        variantRsId = pg["variantRsId"]
        pg_drugs = pg["drugs"]
        if variantRsId and pg_drugs:
            pg_drug_info = pg_drugs[0].get("drug", {})
            drug_name = pg_drug_info.get("name", "")
            inst, err = try_create(
                ASSOCIATION_CLASSES["MutationHasPharamcologicalEffectDrug"],
                subject=Mutation(
                    reference_sequence_identifier=variantRsId,
                ),
                predicate="has_pharmacological_effect",
                object=Drug(drug_name=drug_name),
            )
            record("MutationHasPharamcologicalEffectDrug",
                   "Created" if inst else "FAILED",
                   "external-api (opentargets)", inst, err,
                   f"Mutation({variantRsId}) -> Drug({drug_name})")

# ---------------------------------------------------------------------------
# Associations without a handler implemented above
# ---------------------------------------------------------------------------

NO_DATA = {
    # --- CL-CL -------------------------------------------------------------
    "CellTypeDevelopsFromCellType": "In CL",
    "CellTypeInteractsWithCellType": "Not in CL",
    "CellTypeSubclassOfCellType": "In CL",

    # --- CL-PR -------------------------------------------------------------
    "CellTypeHasPlasmaMembranePartProtein": "In CL",
    "CellTypeLacksPlasmaMembranePartProtein": "In CL",

    # --- In GO? ------------------------------------------------------------
    "ProteinPartOfCellType": "No protein-celltype data",
    "ProteinCapableOfMolecularFunction": "No protein-molecular function data",
    "ProteinInvolvedInBiologicalProcess": "No protein-biological process data",
    "ProteinLocatedInCellularComponent": "No protein-cellular component data",

    # --- From FRMatch? -----------------------------------------------------
    "CellSetExactMatchCellSet": "No cell set matching data",
}


# ---------------------------------------------------------------------------
# Field coverage report
# ---------------------------------------------------------------------------

def print_field_coverage(instance: Any, source: str, cls_name: str) -> None:
    """Print which fields are populated vs None for an entity instance."""
    if instance is None:
        return
    fields = instance.model_fields
    populated = []
    empty = []
    for fname in fields:
        val = getattr(instance, fname, None)
        if val is not None:
            populated.append(fname)
        else:
            empty.append(fname)
    total = len(fields)
    filled = len(populated)
    print(f"     Fields: {filled}/{total} populated")
    if populated:
        print(f"       Populated: {', '.join(populated)}")
    if empty:
        print(f"       Empty:     {', '.join(empty)}")


def print_entity_field_coverage(instance: Any, role: str) -> None:
    """Print field coverage for subject/object entities in an association."""
    if instance is None:
        return
    if isinstance(instance, str):
        print(f"     {role}: str = {instance!r}")
        return
    cls_name = type(instance).__name__
    fields = instance.model_fields
    populated = []
    empty = []
    for fname in fields:
        val = getattr(instance, fname, None)
        if val is not None:
            populated.append(f"{fname}={getattr(instance, fname)!r}")
        else:
            empty.append(fname)
    total = len(fields)
    filled = len(populated)
    print(f"     {role} ({cls_name}): {filled}/{total} fields")
    if populated:
        for p in populated:
            # Truncate long values
            if len(p) > 100:
                p = p[:100] + "..."
            print(f"       + {p}")
    if empty:
        print(f"       - empty: {', '.join(empty)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_report() -> None:
    """Print a structured report of all results."""
    print("=" * 90)
    print("ASSOCIATION CREATION REPORT")
    print("=" * 90)

    created = 0
    partial = 0
    failed = 0
    not_possible = 0
    not_handled = 0

    for name in ALL_ASSOCIATIONS:
        r = results.get(name)
        if r is None:
            status = "NOT RECORDED"
            source = "???"
            notes = ""
        else:
            status = r["status"]
            source = r["source"]
            notes = r["notes"]

        if status == "Created":
            marker = "OK"
            created += 1
        elif status == "PARTIAL":
            marker = "~~"
            partial += 1
        elif status == "FAILED":
            marker = "XX"
            failed += 1
        elif status == "Not possible":
            marker = "--"
            not_possible += 1
        elif status == "Not handled":
            marker = "??"
            not_handled += 1
        else:
            marker = "!!"

        print(f"\n[{marker}] {name}")
        print(f"     Status: {status}")
        print(f"     Source: {source}")
        if notes:
            print(f"     Notes:  {notes}")
        if r and r.get("error"):
            err_lines = r["error"].split("\n")
            print(f"     Error:  {err_lines[0]}")
            for line in err_lines[1:4]:
                print(f"             {line}")

        # Print field coverage for created associations
        if r and r.get("instance"):
            inst = r["instance"]
            print_entity_field_coverage(getattr(inst, "subject", None),
                                        "subject")
            print_entity_field_coverage(getattr(inst, "object", None),
                                        "object")

    print("\n" + "=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"  Created:      {created}")
    print(f"  Partial:      {partial}")
    print(f"  Failed:       {failed}")
    print(f"  Not possible: {not_possible}")
    print(f"  Not handled:  {not_handled}")
    print(f"  Total:        {len(ALL_ASSOCIATIONS)}")

    print("\n" + "=" * 90)
    print("KEY AMBIGUITIES AND FIELD MAPPING NOTES")
    print("=" * 90)
    ambiguities = [
        ("PURL-to-CURIE conversion",
         "Data uses http(s)://purl.obolibrary.org/obo/CL_0000235 but "
         "CellType.ontology_purl validates CL:[0-9]{7} - must convert"),
        ("Gene typed as str in Associations",
         "CellTypeExpressesGene.object is Optional[str] (gene symbol), "
         "not Optional[Gene]. Gene entity class exists but is not used "
         "in associations"),
        ("Species format",
         "Data has 'Homo sapiens' but schema expects CURIEs like "
         "NCBITaxon:9606 for Species entity. CellSet.species and "
         "CellSetDataset.species accept free-text str"),
        ("ccf_located_in vs part_of",
         "Semantic mismatch - HuBMAP uses ccf_located_in but schema "
         "relation is part_of"),
        ("HuBMAP markers on CellType",
         "Schema puts has_characterizing_marker_set on CellSet, "
         "but HuBMAP data has markers on CellType"),
        ("Drug disease ID namespace",
         "Drug diseaseId uses EFO (EFO_0000684) but diseases use MONDO"),
        ("NSForest_markers as string",
         "Stored as \"['SLC12A7', 'OTOGL']\" - needs ast.literal_eval "
         "parsing"),
        ("Publication fields split across sections",
         "From results: PMID/PMCID/DOI. From tuples: title, year, "
         "journal, author_list. Both sections needed for full coverage"),
        ("Protein data split across sources",
         "OpenTargets provides uniprot_id; UniProt API provides "
         "label, function, amino_acids, annotation_score, species, "
         "gene_symbol. Both needed for full Protein coverage"),
        ("Drug fields from OpenTargets",
         "name, mechanism_of_action, trade_names, synonyms (as "
         "exact_synonym), approval_status, study (ctIds). "
         "Drug.disease and Drug.protein derived indirectly"),
        ("CellSet.ontology_purl type",
         "CellSet.ontology_purl is Optional[CellType], not str. "
         "Must pass a CellType instance, not a CURIE string"),
        ("CellSetDataset enrichment from CELLxGENE",
         "CELLxGENE results provide: dataset_name, cell_count, "
         "species (Organism), anatomical_structure (Tissue), "
         "disease_status, cellxgene_collection, and publication link"),
        ("NCBI Gene data not used in associations",
         "Gene entity has fields (gene_symbol, label, uniprot_id, "
         "species, gene_type, refseq_summary, mrna_nm_and_protein_np_"
         "sequences) but Gene is only used as str in associations"),
    ]
    for i, (title, desc) in enumerate(ambiguities, 1):
        print(f"  {i}. {title}")
        print(f"     {desc}")

    print()


def main() -> None:
    print(f"Discovered {len(ALL_ASSOCIATIONS)} Association subclasses "
          f"in ckn_schema.pydantic.ckn_schema\n")

    data = load_all_data()

    from_hubmap(data)
    from_nsforest(data)
    from_author_to_cl(data)
    from_external_api(data)

    # Mark known associations with no data
    for name, reason in NO_DATA.items():
        if name not in results:
            record(name, "Not possible", "N/A", notes=reason)

    # Flag any auto-discovered associations not yet handled
    for name in ALL_ASSOCIATIONS:
        if name not in results:
            record(name, "Not handled", "N/A",
                   notes="New association class - needs handler implementation")

    print_report()

    # Collect tuples from all successfully created Associations and write
    all_tuples = collect_tuples(results)
    if all_tuples:
        output_path = TUPLES_DIRPATH / "cell-kn-schema-tuples.json"
        write_tuples(all_tuples, output_path)
    else:
        print("No tuples generated (no Associations with convertible terms).")


if __name__ == "__main__":
    main()
