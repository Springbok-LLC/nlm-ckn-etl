"""Schema-based tuple writer for the NLM-CKN ETL pipeline.

Reads NSForest results, author-to-CL mappings, silhouette scores,
dataset summaries, and CELLxGENE harvester data, creates Pydantic
Association instances from the ckn-schema, and writes RDF tuples
for loading into ArangoDB via the Java ResultsGraphBuilder.
"""

import ast
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from rdflib.term import Literal, URIRef

from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    Association,
    BinaryGeneSet,
    BiomarkerCombination,
    CellSet,
    CellSetDataset,
    CellType,
    Gene,
    Protein,
    Publication,
)

from LoaderUtilities import (
    DATA_DIRPATH,
    DEPRECATED_TERMS,
    MIN_CLUSTER_SIZE,
    PURLBASE,
    RDFSBASE,
    get_cellxgene_harvester_data,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_results_sources,
    hyphenate,
    load_results,
)

import ckn_schema.pydantic.ckn_schema as ckn_module

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
    "evaluated_in": "OPMI_0000437",
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

# Extra edge annotations from raw data columns (not on the Pydantic
# entity model) for the CS→BMC edge.
CS_BMC_EDGE_COLUMNS = [
    ("precision", "Precision"),
    ("TN", "TN"),
    ("TP", "TP"),
    ("FN", "FN"),
    ("FP", "FP"),
    ("marker_count", "Marker_count"),
]

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

    Generates the core relationship triple, a source quintuple if
    source is provided, vertex annotation triples for subject/object,
    and edge annotation quintuples from EDGE_ANNOTATION_FIELDS.
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

    # Vertex annotation triples
    tuples.extend(entity_to_annotation_triples(subj, s_term, subj_edge_fields))
    tuples.extend(entity_to_annotation_triples(obj, o_term, obj_edge_fields))

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


# ---------------------------------------------------------------------------
# Tuple creation from merged data
# ---------------------------------------------------------------------------


def create_nsforest_tuples(
    nsforest_results: pd.DataFrame,
    summary_data: pd.DataFrame,
    dataset_version_ids: list[str],
    harvester_data: pd.DataFrame | None = None,
) -> list[tuple]:
    """Create tuples from NSForest results. Produces:
    - CellSetDerivesFromAnatomicalStructure
    - CellSetHasCharacterizingMarkerSetBiomarkerCombination
    - CellSetExpressesBinaryGeneSet
    - CellSetHasSourceCellSetDataset
    - GenePartOfBiomarkerCombination (for each marker)
    - BiomarkerCombinationSubclusterOfBinaryGeneSet
    - Plus vertex/edge annotations
    """
    tuples = []

    # Get UBERON terms from summary data
    uberon_terms = [
        t.replace(":", "_").strip()
        for t in str(summary_data.iloc[0]["tissue_ontology_term_id"]).split("|")
    ]

    for _, row in nsforest_results.iterrows():
        uuid = row["uuid"]
        cluster_name = hyphenate(row["clusterName"])
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        markers = parse_string_list(str(row["NSForest_markers"]))
        binary_genes = parse_string_list(str(row["binary_genes"]))

        bmc = BiomarkerCombination(
            markers=" ".join(markers),
            f_beta_score=float(row["f_score"]) if pd.notna(row["f_score"]) else None,
        )
        bgs = BinaryGeneSet(markers=" ".join(binary_genes))
        cell_set = CellSet(
            author_cell_term=cluster_name,
            cell_count=int(cluster_size) if pd.notna(cluster_size) else None,
            biomarker_combination=" ".join(markers),
            binary_gene_set=" ".join(binary_genes),
            expressed_genes=" ".join(binary_genes),
            f_beta_score=float(row["f_score"]) if pd.notna(row["f_score"]) else None,
            silhouette_score=(
                float(row["median"])
                if "median" in row and pd.notna(row.get("median"))
                else None
            ),
        )
        ctx = {"uuid": uuid}

        # CellSet derives_from AnatomicalStructure
        for uberon_term in uberon_terms:
            anat = make_anatomical_structure_from_term(uberon_term)
            assoc = ASSOCIATION_CLASSES["CellSetDerivesFromAnatomicalStructure"](
                subject=cell_set,
                predicate="derives_from",
                object=anat,
            )
            tuples.extend(association_to_tuples(assoc, ctx, source="NSForest"))

        # CellSet expresses BinaryGeneSet
        assoc = ASSOCIATION_CLASSES["CellSetExpressesBinaryGeneSet"](
            subject=cell_set,
            predicate="expresses",
            object=bgs,
        )
        tuples.extend(association_to_tuples(assoc, ctx, source="NSForest"))

        # CellSet has_characterizing_marker_set BiomarkerCombination
        assoc = ASSOCIATION_CLASSES[
            "CellSetHasCharacterizingMarkerSetBiomarkerCombination"
        ](
            subject=cell_set,
            predicate="has_characterizing_marker_set",
            object=bmc,
        )
        tuples.extend(association_to_tuples(assoc, ctx, source="NSForest"))

        # Extra edge annotations from raw data columns on CS→BMC edge
        cs_uri = URIRef(f"{PURLBASE}/CS_{cluster_name}-{uuid}")
        bmc_uri = URIRef(f"{PURLBASE}/BMC_{uuid}")
        pred_uri = URIRef(f"{PURLBASE}/RO_0015004")
        tuples.append(
            (
                cs_uri,
                pred_uri,
                bmc_uri,
                URIRef(f"{PURLBASE}/#source_algorithm"),
                Literal("NSForest-v4.0_dev"),
            )
        )
        for col, attr in CS_BMC_EDGE_COLUMNS:
            if col in row and pd.notna(row[col]):
                tuples.append(
                    (
                        cs_uri,
                        pred_uri,
                        bmc_uri,
                        URIRef(f"{RDFSBASE}#{attr}"),
                        Literal(str(row[col])),
                    )
                )

        # CellSet source CellSetDataset (for each dataset_version_id)
        for dvid in dataset_version_ids:
            harvester_row = None
            if harvester_data is not None and not harvester_data.empty:
                match = harvester_data[harvester_data["dataset_version_id"] == dvid]
                if not match.empty:
                    harvester_row = match.iloc[0]

            csd = make_cell_set_dataset(
                dvid,
                summary_data=summary_data,
                harvester_row=harvester_row,
            )
            assoc = ASSOCIATION_CLASSES["CellSetHasSourceCellSetDataset"](
                subject=cell_set,
                predicate="source",
                object=csd,
            )
            tuples.extend(association_to_tuples(assoc, ctx, source="NSForest"))

        # Gene part_of BiomarkerCombination (for each marker)
        for gene_symbol in markers:
            gene = make_gene(gene_symbol)
            assoc = ASSOCIATION_CLASSES["GenePartOfBiomarkerCombination"](
                subject=gene,
                predicate="part_of",
                object=bmc,
            )
            tuples.extend(association_to_tuples(assoc, ctx, source="NSForest"))

        # BiomarkerCombination subcluster_of BinaryGeneSet
        assoc = ASSOCIATION_CLASSES["BiomarkerCombinationSubclusterOfBinaryGeneSet"](
            subject=bmc,
            predicate="subcluster_of",
            object=bgs,
        )
        tuples.extend(association_to_tuples(assoc, ctx, source="NSForest"))

    return tuples


def create_author_to_cl_tuples(
    author_to_cl_results: pd.DataFrame,
    dataset_version_ids: list[str],
    harvester_data: pd.DataFrame | None = None,
) -> list[tuple]:
    """Create tuples from author-to-CL mapping results. Produces:
    - CellTypePartOfAnatomicalStructure
    - CellTypeHasExemplarDataCellSetDataset
    - CellSetComposedPrimarilyOfCellType
    - CellTypeExpressesGene (for each marker + binary gene)
    """
    tuples = []

    for _, row in author_to_cl_results.iterrows():
        uuid = row["uuid"]
        cluster_size = row["clusterSize"]
        if cluster_size < MIN_CLUSTER_SIZE:
            continue

        cell_type = make_cell_type(row)
        anat = make_anatomical_structure(row)
        if cell_type is None or anat is None:
            continue

        cl_term = curie_to_term(cell_type.ontology_purl)
        uberon_term = curie_to_term(anat.ontology_purl)
        if cl_term in DEPRECATED_TERMS:
            print(f"Warning: CL term {cl_term} deprecated")
        if uberon_term in DEPRECATED_TERMS:
            print(f"Warning: UBERON term {uberon_term} deprecated")

        author_cell_set = hyphenate(str(row.get("author_cell_set", "")))
        markers = parse_string_list(str(row.get("NSForest_markers", "[]")))
        binary_genes = parse_string_list(str(row.get("binary_genes", "[]")))

        doi = row.get("DOI")
        collection_id = row.get("collection_id")
        collection_version_id = row.get("collection_version_id")
        dataset_version_id = row.get("dataset_version_id")

        cell_set = make_cell_set(
            row,
            cell_type=cell_type,
            uberon_curie=anat.ontology_purl,
            doi=str(doi) if pd.notna(doi) else None,
            markers=markers,
            binary_genes=binary_genes,
            collection_id=str(collection_id) if pd.notna(collection_id) else None,
            dataset_version_id=(
                str(dataset_version_id) if pd.notna(dataset_version_id) else None
            ),
        )
        ctx = {"uuid": uuid}

        # CellType part_of AnatomicalStructure
        assoc = ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
            subject=cell_type,
            predicate="part_of",
            object=anat,
        )
        tuples.extend(association_to_tuples(assoc, ctx, source="Manual Mapping"))

        # CellSet composed_primarily_of CellType
        assoc = ASSOCIATION_CLASSES["CellSetComposedPrimarilyOfCellType"](
            subject=cell_set,
            predicate="composed_primarily_of",
            object=cell_type,
        )
        t = association_to_tuples(assoc, ctx, source="Manual Mapping")
        tuples.extend(t)

        # Edge annotations on CS→CellType: Match and Mapping_method
        cs_uri = URIRef(f"{PURLBASE}/CS_{author_cell_set}-{uuid}")
        ct_uri = URIRef(f"{PURLBASE}/{cl_term}")
        pred_uri = URIRef(f"{PURLBASE}/RO_0002473")
        match_val = row.get("match")
        if pd.notna(match_val):
            tuples.append(
                (
                    cs_uri,
                    pred_uri,
                    ct_uri,
                    URIRef(f"{RDFSBASE}#Match"),
                    Literal(str(match_val)),
                )
            )
        method_val = row.get("mapping_method")
        if pd.notna(method_val):
            tuples.append(
                (
                    cs_uri,
                    pred_uri,
                    ct_uri,
                    URIRef(f"{RDFSBASE}#Mapping_method"),
                    Literal(str(method_val)),
                )
            )

        # CellType has_exemplar_data CellSetDataset
        for dvid in dataset_version_ids:
            harvester_row = None
            if harvester_data is not None and not harvester_data.empty:
                match_df = harvester_data[harvester_data["dataset_version_id"] == dvid]
                if not match_df.empty:
                    harvester_row = match_df.iloc[0]

            csd = make_cell_set_dataset(
                dvid,
                harvester_row=harvester_row,
                doi=str(doi) if pd.notna(doi) else None,
                collection_id=(str(collection_id) if pd.notna(collection_id) else None),
                collection_version_id=(
                    str(collection_version_id)
                    if pd.notna(collection_version_id)
                    else None
                ),
            )
            assoc = ASSOCIATION_CLASSES["CellTypeHasExemplarDataCellSetDataset"](
                subject=cell_type,
                predicate="has_exemplar_data",
                object=csd,
            )
            tuples.extend(association_to_tuples(assoc, ctx, source="Manual Mapping"))

            # CellSetDataset source Publication
            pub = make_publication(row)
            if pub is not None:
                assoc = ASSOCIATION_CLASSES["CellSetDatasetHasSourcePublication"](
                    subject=csd,
                    predicate="source",
                    object=pub,
                )
                tuples.extend(
                    association_to_tuples(assoc, ctx, source="Manual Mapping")
                )

        # CellType expresses Gene (for each marker and binary gene)
        for gene_symbol in markers + binary_genes:
            gene = make_gene(gene_symbol)
            assoc = ASSOCIATION_CLASSES["CellTypeExpressesGene"](
                subject=cell_type,
                predicate="expresses",
                object=gene,
            )
            tuples.extend(association_to_tuples(assoc, ctx, source="Manual Mapping"))

    return tuples


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def process_dataset(
    nsforest_path: Path,
    mapping_path: list[Path],
    scores_path: list[Path],
    summary_path: list[Path],
    dataset_version_ids: list[str],
    harvester_data: pd.DataFrame | None = None,
) -> list[tuple]:
    """Process a single dataset: load, merge, and create tuples."""
    all_tuples = []

    # Load NSForest results
    nsforest_results = load_results(nsforest_path).sort_values(
        "clusterName", ignore_index=True
    )

    # Merge silhouette scores if available
    if scores_path:
        cluster_header = nsforest_results.loc[0, "cluster_header"]
        silhouette_scores = load_results(scores_path[0]).sort_values(
            cluster_header, ignore_index=True
        )
        nsforest_results = nsforest_results.merge(
            silhouette_scores[[cluster_header, "median"]].copy(),
            left_on="clusterName",
            right_on=cluster_header,
        )

    # Load summary data
    summary_data = load_results(summary_path[0]) if summary_path else pd.DataFrame()

    # Create NSForest tuples
    print(f"Creating NSForest tuples from {nsforest_path.name}")
    nsforest_tuples = create_nsforest_tuples(
        nsforest_results, summary_data, dataset_version_ids, harvester_data
    )
    all_tuples.extend(nsforest_tuples)

    # Create author-to-CL tuples if mapping exists
    if mapping_path:
        author_to_cl_path = mapping_path[0]
        author_to_cl_results = (
            load_results(author_to_cl_path)
            .sort_values("author_cell_set", ignore_index=True)
            .drop(columns=["uuid"])
        )
        # Merge with NSForest results to get uuid, clusterSize, markers
        author_to_cl_results = author_to_cl_results.merge(
            nsforest_results[
                [
                    "clusterName",
                    "clusterSize",
                    "NSForest_markers",
                    "binary_genes",
                    "uuid",
                ]
            ].copy(),
            left_on="author_cell_set",
            right_on="clusterName",
        )
        print(f"Creating author-to-CL tuples from {author_to_cl_path.name}")
        author_to_cl_tuples = create_author_to_cl_tuples(
            author_to_cl_results, dataset_version_ids, harvester_data
        )
        all_tuples.extend(author_to_cl_tuples)
    else:
        print(f"No mapping file for {nsforest_path.name}")

    return all_tuples


def main():
    """Run the schema-based tuple writer pipeline."""
    results_sources = get_results_sources()
    harvester_data = get_cellxgene_harvester_data(results_sources)
    file_paths = get_dataset_file_paths(results_sources)
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)

    nsforest_paths = file_paths["nsforest_paths"]
    mapping_paths = file_paths["mapping_paths"]
    scores_paths = file_paths["scores_paths"]
    summary_paths = file_paths["summary_paths"]

    for nsforest_path, mapping_path, scores_path, summary_path, dvids in zip(
        nsforest_paths,
        mapping_paths,
        scores_paths,
        summary_paths,
        dataset_version_id_lists,
    ):
        dataset_tuples = process_dataset(
            nsforest_path,
            mapping_path,
            scores_path,
            summary_path,
            dvids,
            harvester_data,
        )
        if dataset_tuples:
            output_name = nsforest_path.name.replace(".csv", ".json")
            output_path = TUPLES_DIRPATH / output_name
            write_tuples(dataset_tuples, output_path)


if __name__ == "__main__":
    main()
