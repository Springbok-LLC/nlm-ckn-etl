import ast
import boto3
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import random
import re
import string
from time import sleep

from lxml import etree
import pandas as pd
import scanpy as sc

from rdflib.term import BNode
from urllib.parse import urlparse

from UniProtIdMapper import (
    submit_id_mapping,
    check_id_mapping_results_ready,
    get_id_mapping_results_link,
    get_id_mapping_results_search,
)

import ProductionDataSpecification as spec

ALPHABET = string.ascii_lowercase + string.digits

OPENTARGETS_RESOURCES = [
    "diseases",
    "drugs",
    "pharmacogenetics",
]
PURLBASE = "http://purl.obolibrary.org/obo"
RDFSBASE = "http://www.w3.org/1999/02/22-rdf-syntax-ns"

OWL_NS = "{http://www.w3.org/2002/07/owl#}"
OBO_IN_OWL_NS = "{http://www.geneontology.org/formats/oboInOwl#}"
RDF_NS = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"

DATA_DIRPATH = Path(__file__).resolve().parents[2] / "data"
BIOMART_DIRPATH = DATA_DIRPATH / "biomart"
GENE_MAPPING_PATH = BIOMART_DIRPATH / "gene_mapping.csv"
_S3_BUCKET = os.getenv("S3_BUCKET", "")
_S3_GENE_MAPPING_KEY = "cache/biomart/gene_mapping.csv"

DEFAULT_RUN_NAME = "full"


@dataclass(frozen=True)
class RunConfig:
    """Per-run configuration describing which sources feed the
    pipeline and where its outputs land."""

    run_name: str
    results_dir: Path  # flat directory of extracted release zip contents
    external_dir: Path
    tuples_dir: Path
    hubmap_urls: list = field(default_factory=list)

    @classmethod
    def load(cls, run_name):
        """Resolve paths for a named run.

        ``results_dir`` (``data/results-<run_name>/``) is expected to contain
        the extracted contents of the nlm-ckn release zip, including
        ``hubmap_urls.txt``.  No JSON config file is required.
        """
        results_dir = DATA_DIRPATH / f"results-{run_name}"
        hubmap_urls_path = results_dir / "hubmap_urls.txt"
        hubmap_urls = []
        if hubmap_urls_path.exists():
            hubmap_urls = [
                line.strip().strip('"').strip("'")
                for line in hubmap_urls_path.read_text().splitlines()
                if line.strip()
            ]
        return cls(
            run_name=run_name,
            results_dir=results_dir,
            external_dir=DATA_DIRPATH / f"external-{run_name}",
            tuples_dir=DATA_DIRPATH / f"tuples-{run_name}",
            hubmap_urls=hubmap_urls,
        )


_CURRENT_RUN = None


def set_current_run(run_name=None):
    """Set the process-wide run config. Resolution order:

    1. ``run_name`` argument, if provided
    2. ``CKN_RUN`` environment variable, if set
    3. :data:`DEFAULT_RUN_NAME`

    Returns the resolved :class:`RunConfig`.
    """
    global _CURRENT_RUN
    name = run_name or os.environ.get("CKN_RUN") or DEFAULT_RUN_NAME
    _CURRENT_RUN = RunConfig.load(name)
    return _CURRENT_RUN


def get_current_run():
    """Return the current :class:`RunConfig`, initializing it from the
    env var or default run if not already set."""
    if _CURRENT_RUN is None:
        set_current_run()
    return _CURRENT_RUN


with open(DATA_DIRPATH / "obo" / "deprecated_terms.txt", "r") as fp:
    DEPRECATED_TERMS = fp.read().splitlines()

# Re-exported from ProductionDataSpecification so the naming/column spec and the readers
# share one definition (see ProductionDataValidator).
MIN_CLUSTER_SIZE = spec.MIN_CLUSTER_SIZE

URIREF_PATTERN = re.compile(r"/obo/([A-Za-z]*)_([A-Za-z0-9-+]*)")


def parse_term(term, ro=None):
    """Parse an rdflib term first as an URIRef that identifies a
    class, including relationship classes, then a predicate, BNode, or
    Literal.

    Parameters
    ----------
    term : rdflib.term.BNode|Literal|URIRef | str
        An rdflib term: BNode, Literal, or URIRef, or equivalent string
    ro : None | dict
        A dictionary mapping relationship ontology terms to labels

    Returns
    -------
    tuple
        Contains ontology identifier, number, and term, label or
        literal value, and type ('class', 'predicate', or 'literal'),
        in which any element of the tuple may also be None
    """
    # Parse then match as URL
    path = urlparse(term).path
    fragment = urlparse(term).fragment
    match = URIREF_PATTERN.match(path)
    if match is not None:
        # Matched as URL
        oid = match.group(1)
        if oid == "GOREL":
            # Identifier not found in the Ontology Lookup Service
            print(f"Invalid Ontology ID: 'GOREL' for term: {term}")
            return None, None, None, None, None

        number = match.group(2)
        if len(oid) == 0 or len(number) == 0:
            print(f"Did not match ontology id or number for term: {term}")
            return None, None, None, None, None

        term = f"{oid}_{number}"

        if ro is not None and term in ro:
            # Lookup label for relationship ontology term
            return oid, number, term, ro[term], "class"

        else:
            return oid, number, term, None, "class"

    elif fragment != "":
        # Parsed as URL with a fragment, so assume fragment is a
        # predicate
        return None, None, None, fragment, "predicate"

    elif isinstance(term, BNode):
        # Create pseudo ontology identifier, number, and term for a
        # BNode
        oid = "BNode"
        number = Path(path).stem
        term = f"{oid}_{number}"
        return oid, number, term, None, "class"

    else:
        # Parsed as URL without a fragment, so assume stem is a
        # literal
        return None, None, None, Path(path).stem, "literal"


def get_cellxgene_harvester_data(results_dir=None):
    """Get and concatenate cellxgene-harvester data from the flat results dir.

    Parameters
    ----------
    results_dir : Path, optional
        Flat directory of extracted release zip contents.  Defaults to the
        current run config's ``results_dir``.

    Returns
    -------
    harvester_data : pd.DataFrame
        Dataframe containing the concatenated cellxgene-harvester data
    """
    if results_dir is None:
        results_dir = get_current_run().results_dir

    results_dir = Path(results_dir)
    harvester_paths = sorted(results_dir.glob(spec.HARVESTER_GLOB))

    if harvester_paths:
        return pd.concat([pd.read_csv(p) for p in harvester_paths])
    return pd.DataFrame()


def get_uberon_root_map(results_dir=None):
    """Get the organ to UBERON root/descendant mapping from the flat results dir.

    Reads each ``uberon_<organ>.csv`` the cellxgene-harvester emits, keying it
    by the organ in its filename, which is the value a dataset summary carries
    in its ``organ`` column.

    Parameters
    ----------
    results_dir : Path, optional
        Flat directory of extracted release zip contents.  Defaults to the
        current run config's ``results_dir``.

    Returns
    -------
    dict
        Mapping from organ to ``{"roots": [(obo_id, label), ...], "terms":
        set[obo_id]}``, where ``terms`` holds every term of the organ, root
        and descendant alike.
    """
    if results_dir is None:
        results_dir = get_current_run().results_dir

    results_dir = Path(results_dir)

    root_map = {}
    for path in sorted(Path(results_dir).glob(spec.UBERON_GLOB_RECURSIVE)):
        organ = spec.organ_of_uberon_path(path)
        uberon_data = pd.read_csv(path)
        roots = [
            (row["obo_id"], row["label"])
            for _, row in uberon_data.iterrows()
            if row["level"] == spec.UBERON_ROOT_LEVEL
        ]
        if not roots:
            print(f"Warning: no root term in {path.name}")
            continue
        root_map[organ] = {"roots": roots, "terms": set(uberon_data["obo_id"])}

    return root_map


def resolve_root_uberon_term(organ, tissue_terms, root_map):
    """Resolve the single root UBERON term a dataset's cell sets derive from.

    A dataset is sampled from one organ, so its cell sets connect to that
    organ's root term rather than to each of the pipe-delimited descendant
    terms the summary carries (Springbok-LLC/nlm-ckn-etl#38).

    Parameters
    ----------
    organ : str
        The summary's ``organ`` value.
    tissue_terms : list[str]
        The summary's ``tissue_ontology_term_id`` values, as CURIEs.
    root_map : dict
        Mapping returned by :func:`get_uberon_root_map`.

    Returns
    -------
    str or None
        The root UBERON CURIE, or None if the organ has neither a harvester
        table nor an override, in which case the caller keeps the dataset's
        own tissue terms.
    """
    organ = spec.normalize_organ(organ)
    entry = root_map.get(organ)

    if entry is None:
        root_term = spec.ORGAN_ROOT_OVERRIDES.get(organ)
        if root_term is None:
            print(f"Warning: no UBERON root term for organ {organ}")
        return root_term

    # An organ whose harvester table was built from more than one query has
    # more than one root (respiratory system also has nose).  A dataset
    # sampled from one of those roots connects to it directly; any other
    # dataset connects to the root named by the organ.
    roots = entry["roots"]
    root_ids = [obo_id for obo_id, _ in roots]
    for term in tissue_terms:
        if term in root_ids:
            return term

    for term in tissue_terms:
        if term not in entry["terms"]:
            print(f"Warning: tissue term {term} not among the terms of {organ}")

    for obo_id, label in roots:
        if spec.normalize_organ(label) == organ:
            return obo_id

    return root_ids[0]


def resolve_summary_root_uberon_term(summary_data, root_map):
    """Resolve the root UBERON term of a dataset summary, or None.

    Wraps :func:`resolve_root_uberon_term` for the writers, which hold the
    summary as a dataframe with ``organ`` and ``tissue_ontology_term_id``
    columns.
    """
    if summary_data is None or summary_data.empty:
        return None

    row = summary_data.iloc[0]
    if "organ" not in summary_data.columns or pd.isna(row["organ"]):
        print("Warning: dataset summary has no organ, so no UBERON root term")
        return None

    tissue_terms = []
    if "tissue_ontology_term_id" in summary_data.columns and pd.notna(
        row["tissue_ontology_term_id"]
    ):
        tissue_terms = [
            t.strip().replace("_", ":")
            for t in str(row["tissue_ontology_term_id"]).split("|")
            if t.strip()
        ]

    return resolve_root_uberon_term(row["organ"], tissue_terms, root_map)


def get_dataset_file_paths(results_dir=None):
    """Get all NSForest results paths and their companion file paths from
    the flat release zip directory.

    The release zip stores all files at the top level using a stable naming
    convention.  For each ``results_ensg_*.csv`` file the companion files are
    located by substituting the ``results_ensg`` prefix:

    - mapping:  ``results_ensg`` → ``cluster_cid_mapping`` (sparse — only the
      reference dataset per organ has one, so most results sets have none)
    - scores:   ``results_ensg`` → ``silhouette_fscore_summary``
    - summary:  ``results_ensg`` → ``master_dataset_summary``

    Parameters
    ----------
    results_dir : Path, optional
        Flat directory of extracted release zip contents.  Defaults to the
        current run config's ``results_dir``.

    Returns
    -------
    file_paths : dict
        ``nsforest_paths`` — list of Path
        ``mapping_paths``  — list of list[Path] (one per nsforest file)
        ``scores_paths``   — list of list[Path]
        ``summary_paths``  — list of list[Path]
    """
    if results_dir is None:
        results_dir = get_current_run().results_dir

    results_dir = Path(results_dir)
    nsforest_paths = sorted(results_dir.glob(spec.NSFOREST_GLOB))

    mapping_paths = []
    scores_paths = []
    summary_paths = []

    for p in nsforest_paths:
        mapping_paths.append(
            list(
                results_dir.glob(
                    "**/" + spec.companion_basename(p.name, spec.MAPPING_PREFIX)
                )
            )
        )
        scores_paths.append(
            list(
                results_dir.glob(
                    "**/" + spec.companion_basename(p.name, spec.SILHOUETTE_PREFIX)
                )
            )
        )
        summary_paths.append(
            list(
                results_dir.glob(
                    "**/" + spec.companion_basename(p.name, spec.SUMMARY_PREFIX)
                )
            )
        )

    # Warn for any results file missing its summary — get_dataset_version_id_lists
    # reads the dataset_version_id column from the summary, so its absence fails
    # later with a confusing traceback.  A missing cluster_cid_mapping is NOT an
    # error: it is sparse (only the reference dataset per organ has one), and
    # MappingTupleWriter simply skips results sets without a mapping.
    for p, sp in zip(nsforest_paths, summary_paths):
        if not sp:
            print(
                f"WARNING: No master_dataset_summary found for {p.name} — "
                f"dataset version id lookup will fail."
            )

    # Cross-check against the manifest so missing results files are caught at
    # extraction time rather than discovered implicitly through absent output.
    manifest_path = results_dir / spec.MANIFEST_NAME
    if manifest_path.exists():
        try:
            manifest = pd.read_csv(manifest_path)
            expected = {
                row["filename"]
                for _, row in manifest.iterrows()
                if str(row["filename"]).startswith(f"{spec.NSFOREST_PREFIX}_")
                and str(row["filename"]).endswith(".csv")
            }
            found = {p.name for p in nsforest_paths}
            missing = expected - found
            for name in sorted(missing):
                print(
                    f"WARNING: {name} is listed in master_s3_manifest.csv "
                    f"but was not found in {results_dir.name}/"
                )
        except Exception as exc:
            print(f"WARNING: Could not validate against master_s3_manifest.csv: {exc}")

    return {
        "nsforest_paths": nsforest_paths,
        "mapping_paths": mapping_paths,
        "scores_paths": scores_paths,
        "summary_paths": summary_paths,
    }


def get_dataset_version_id_lists(file_paths):
    """Get dataset version id lists for each results source from the dataset
    summary file's ``dataset_version_id`` column.

    A single results set can correspond to multiple datasets (e.g. Jorstad,
    whose summary has one row per dataset), so every row of every summary file
    paired with the results set contributes a dataset version id.

    Parameters
    ----------
    file_paths: dict
        Dictionary containing lists of file paths

    Returns
    -------
    dataset_version_id_lists : list(list)
        List of the dataset version identifier lists corresponding to the
        datasets used to generate each NSForest results path
    """
    dataset_version_id_lists = []

    for summary_path, nsforest_path in zip(
        file_paths["summary_paths"],
        file_paths["nsforest_paths"],
    ):
        dataset_version_ids = []
        for p in summary_path:
            summary_data = pd.read_csv(p)
            if "dataset_version_id" in summary_data.columns:
                dataset_version_ids.extend(
                    summary_data["dataset_version_id"].dropna().astype(str).tolist()
                )

        if not dataset_version_ids:
            raise Exception(f"No dataset version id found for {nsforest_path}")

        dataset_version_id_lists.append(dataset_version_ids)

    return dataset_version_id_lists


def get_dataset_organs_map(results_dir=None):
    """Map each source ``dataset_version_id`` to the organs it was filtered for.

    A CELLxGENE source dataset is filtered for one organ per NSForest results
    set, so the same ``dataset_version_id`` can appear in several summaries
    (one per organ).  Writers that mint a cell set dataset vertex per filtered
    dataset (keyed on source id plus organ) need this map to reproduce the same
    identifiers the summary-driven writers use — most importantly
    ``CellxGeneTupleWriter``, whose CELLxGENE metadata carries no organ.

    Parameters
    ----------
    results_dir : Path, optional
        Flat directory of extracted release zip contents.  Defaults to the
        current run config's ``results_dir``.

    Returns
    -------
    dict
        Mapping from ``dataset_version_id`` to the set of organ values found
        across the summaries.  A dataset with no organ contributes an empty
        set.
    """
    if results_dir is None:
        results_dir = get_current_run().results_dir

    file_paths = get_dataset_file_paths(results_dir)
    organs_by_dvid = {}
    for summary_path in file_paths["summary_paths"]:
        for p in summary_path:
            summary_data = pd.read_csv(p)
            if "dataset_version_id" not in summary_data.columns:
                continue
            has_organ = "organ" in summary_data.columns
            for _, row in summary_data.iterrows():
                dvid = row["dataset_version_id"]
                if pd.isna(dvid):
                    continue
                dvid = str(dvid)
                organ = row["organ"] if has_organ else None
                organs = organs_by_dvid.setdefault(dvid, set())
                if organ is not None and pd.notna(organ) and str(organ).strip():
                    organs.add(str(organ))
    return organs_by_dvid


def get_unique_gene_names_and_ids(nsforest_paths):
    """Get unique gene names, and Ensembl and Entrez ids from all NSForest
    results.

    Parameters
    ----------
    nsforest_paths : list(Path)
        List of NSForest results paths

    Returns:
    gene_data : dict
        Dictionary contains names and ids
    """
    gene_names = set()
    for nsforest_path in nsforest_paths:
        print(f"Loading NSForest results from {nsforest_path}")
        nsforest_results = load_results(nsforest_path).sort_values(
            "clusterName", ignore_index=True
        )
        gene_names |= set(collect_unique_gene_names(nsforest_results))

    gene_ensembl_ids = collect_unique_gene_ensembl_ids(gene_names)
    gene_entrez_ids = collect_unique_gene_entrez_ids(gene_names)

    return {
        "gene_names": gene_names,
        "gene_ensembl_ids": gene_ensembl_ids,
        "gene_entrez_ids": gene_entrez_ids,
    }


def get_cl_terms(author_to_cl_paths):
    """Create a set of clean CL terms from the given author to CL paths.

    Parameters
    ----------
    author_to_cl_pahts : list(str)
        List containing paths to author to CL mapping

    Returns
    -------
    set(str)
        Set of clean CL terms
    """
    cl_terms = set()

    for author_to_cl_path in author_to_cl_paths:
        if author_to_cl_path == []:
            continue
        author_to_cl_results = load_results(author_to_cl_path[0])

        cl_terms.update(
            author_to_cl_results.loc[
                author_to_cl_results["cell_ontology_id"].str.contains("CL"),
                "cell_ontology_id",
            ]
            .str.replace("http://purl.obolibrary.org/obo/", "")
            .str.replace("https://purl.obolibrary.org/obo/", "")
        )

    return cl_terms


def get_uuid():
    """Get an eight character random string.

    Parameters
    ----------
    None

    Returns
    -------
    An eight character random string.
    """
    return "".join(random.choices(ALPHABET, k=12))


def as_citation_part(value):
    """Return a citation part as a clean string, or "" if it is missing.

    Accepts values as they come from either JSON metadata or a CSV row,
    so a NaN reads as missing rather than as the string "nan", and a year
    that pandas typed as a float reads as 2023 rather than as 2023.0.

    Parameters
    ----------
    value : Any
        A citation part: an author, a year, or a journal.

    Returns
    -------
    str
        The part as a string, or "" if it is missing.
    """
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def build_citation(first_author, year, journal=None):
    """Build a citation from author, year, and journal.

    The single source of the citation format. CELLxGENE metadata and the
    dataset summary and harvester CSV files each carry these fields, and
    each writes a citation onto the same cell set dataset vertex, so all
    three must spell it the same way.

    Parameters
    ----------
    first_author : Any
        Family name of the first author.
    year : Any
        Year of publication.
    journal : Any
        Journal of publication, omitted from the citation when absent.

    Returns
    -------
    str or None
        A citation, such as "Sikkema (2023) Nat Med", or None unless
        both an author and a year are known.
    """
    author = as_citation_part(first_author)
    year = as_citation_part(year)
    if not author or not year:
        return None
    citation = f"{author} ({year})"
    journal = as_citation_part(journal)
    if journal:
        citation += f" {journal}"
    return citation


def build_dataset_label(citation, dataset_name):
    """Build the name by which a cell set dataset is known.

    A publication can contribute several datasets, so a citation alone
    does not identify one. Qualifying the citation with the dataset name
    does, provided both are known, and the label falls back to whichever
    is known so that every dataset is named as uniquely as its metadata
    allows.

    Parameters
    ----------
    citation : Any
        Citation of the publication the dataset was reported in.
    dataset_name : Any
        Name of the dataset.

    Returns
    -------
    str or None
        A label, such as "Sikkema (2023) Nat Med - Lung, 3' v2", or None
        if neither a citation nor a dataset name is known.
    """
    citation = as_citation_part(citation)
    dataset_name = as_citation_part(dataset_name)
    if citation and dataset_name:
        return f"{citation} - {dataset_name}"
    return citation or dataset_name or None


def load_results(results_path):
    """Load results CSV file and append a UUID.

    Parameters
    ----------
    results_Path : Path
        Path of results CSV file

    Returns
    -------
    results : pd.DataFrame
        DataFrame containing results
    """
    results = pd.read_csv(results_path)
    if "uuid" not in results.columns:
        print(f"Add UUID column to results CSV file {results_path.name}")
        results["uuid"] = [get_uuid() for idx in results.index]
        results.to_csv(results_path)
    return results


def hyphenate(iname):
    """Replace spaces, underscores, commas and forward slashes with
    hyphens, but only one.

    Parameters
    ----------
    iname : str
        Input name

    Returns
    -------
    oname : str
        Output name
    """
    cname = iname
    for c in [" ", "_", ",", "/"]:
        cname = cname.replace(c, "-")
        oname = cname.replace("--", "-")
        while cname != oname:
            cname = oname
            oname = oname.replace("--", "-")
    return oname


def get_gene_names_and_ensembl_and_entrez_ids():
    """Get gene names, and Ensembl and Entrez ids from a cached file,
    or query BioMart and cache the result.

    Parameters
    ----------
    None

    Returns
    -------
    gene_names_and_ids : pd.DataFrame
        DataFrame with columns containing gene names, and Ensembl and
        Entrez ids
    """
    # Half the typical Ensembl release interval (about six months). Note that
    # the GENCODE release interval is years.
    max_fetch_age_hours = 2160.0
    if GENE_MAPPING_PATH.exists():
        age_hours = (
            datetime.now(timezone.utc)
            - datetime.fromtimestamp(GENE_MAPPING_PATH.stat().st_mtime, tz=timezone.utc)
        ).total_seconds() / 3600
        if age_hours <= max_fetch_age_hours:
            print(
                f"Loading gene mapping from {GENE_MAPPING_PATH} ({age_hours:.1f}h old)"
            )
            gene_names_and_ids = pd.read_csv(GENE_MAPPING_PATH, index_col=0)
            gene_names_and_ids["entrezgene_id"] = gene_names_and_ids[
                "entrezgene_id"
            ].astype(str)
            return gene_names_and_ids
        print(
            f"Local gene mapping is {age_hours:.1f}h old"
            f" (threshold: {max_fetch_age_hours}h) — re-fetching"
        )
    if _S3_BUCKET:
        try:
            s3 = boto3.client("s3")
            head = s3.head_object(Bucket=_S3_BUCKET, Key=_S3_GENE_MAPPING_KEY)
            last_modified = head["LastModified"]  # timezone-aware datetime
            age_hours = (
                datetime.now(timezone.utc) - last_modified
            ).total_seconds() / 3600
            if age_hours <= max_fetch_age_hours:
                BIOMART_DIRPATH.mkdir(parents=True, exist_ok=True)
                s3.download_file(
                    _S3_BUCKET, _S3_GENE_MAPPING_KEY, str(GENE_MAPPING_PATH)
                )
                print(
                    f"Loaded gene mapping from s3://{_S3_BUCKET}/{_S3_GENE_MAPPING_KEY}"
                    f" ({age_hours:.1f}h old)"
                )
                gene_names_and_ids = pd.read_csv(GENE_MAPPING_PATH, index_col=0)
                gene_names_and_ids["entrezgene_id"] = gene_names_and_ids[
                    "entrezgene_id"
                ].astype(str)
                return gene_names_and_ids
            print(
                f"Gene mapping cache is {age_hours:.1f}h old"
                f" (threshold: {max_fetch_age_hours}h) — re-fetching"
            )
        except (NoCredentialsError, PartialCredentialsError) as exc:
            print(
                f"WARNING: S3 credential error for"
                f" s3://{_S3_BUCKET}/{_S3_GENE_MAPPING_KEY}: {exc}; falling back to source"
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                print(
                    f"WARNING: S3 head_object failed for"
                    f" s3://{_S3_BUCKET}/{_S3_GENE_MAPPING_KEY}: {exc}; falling back to source"
                )
            else:
                print(
                    f"Gene mapping not in S3 (s3://{_S3_BUCKET}/{_S3_GENE_MAPPING_KEY});"
                    " fetching from source"
                )

    print("Getting gene names, and Ensembl and Entrez ids from BioMart")
    biomart_retries = 3
    biomart_retry_delay = 30  # seconds
    for attempt in range(1, biomart_retries + 1):
        try:
            gene_names_and_ids = (
                sc.queries.biomart_annotations(
                    "hsapiens",
                    ["external_gene_name", "ensembl_gene_id", "entrezgene_id"],
                    use_cache=True,
                )
                .dropna()
                .drop_duplicates()
            )
            break
        except Exception as exc:
            if attempt < biomart_retries:
                print(
                    f"BioMart query failed (attempt {attempt}/{biomart_retries}): {exc}. "
                    f"Retrying in {biomart_retry_delay}s..."
                )
                sleep(biomart_retry_delay)
            else:
                raise
    gene_names_and_ids["entrezgene_id"] = (
        gene_names_and_ids["entrezgene_id"].astype(int).astype(str)
    )
    BIOMART_DIRPATH.mkdir(parents=True, exist_ok=True)
    gene_names_and_ids.to_csv(GENE_MAPPING_PATH)
    if _S3_BUCKET:
        try:
            boto3.client("s3").upload_file(
                str(GENE_MAPPING_PATH), _S3_BUCKET, _S3_GENE_MAPPING_KEY
            )
            print(f"Cached gene mapping to s3://{_S3_BUCKET}/{_S3_GENE_MAPPING_KEY}")
        except Exception as exc:
            print(
                f"WARNING: Failed to cache gene mapping to"
                f" s3://{_S3_BUCKET}/{_S3_GENE_MAPPING_KEY}: {exc}"
            )
    return gene_names_and_ids


def get_gene_name_to_ensembl_ids_map():
    """Get gene name to Ensembl ids map.

    Parameters
    ----------
    None

    Returns
    -------
    gene_name_to_ensembl_ids : pd.DataFrame
        DataFrame indexed by gene name containing gene Ensembl id
    """
    print("Creating gene name to Ensembl ids map")
    gene_names_and_ids = get_gene_names_and_ensembl_and_entrez_ids()
    gene_name_to_ensembl_ids = gene_names_and_ids.set_index("external_gene_name")
    return gene_name_to_ensembl_ids


def map_gene_name_to_ensembl_ids(name, gene_name_to_ensembl_ids):
    """Map a gene name to a gene Ensembl id list.

    Parameters
    ----------
    name : str
        Gene name
    gene_name_to_ensembl_ids : pd.DataFrame
        DataFrame indexed by gene name containing gene Ensembl id

    Returns
    -------
    list
        Gene Ensembl ids
    """
    if name in gene_name_to_ensembl_ids.index:
        ids = gene_name_to_ensembl_ids.loc[name, "ensembl_gene_id"]
        if isinstance(ids, pd.core.series.Series):
            ids = ids.to_list()
        else:
            ids = [ids]
        # print(f"Mapped gene name {name} to Ensembl ids {ids}")
    else:
        print(f"Could not find gene Ensembl ids for gene name: {name}")
        ids = []
    return ids


def get_gene_ensembl_id_to_names_map():
    """Map gene Ensembl id to names.

    Parameters
    ----------
    None

    Returns
    -------
    gene_ensembl_id_to_names : pd.DataFrame
        DataFrame indexed by gene Ensembl ids containing gene names
    """
    print("Creating gene Ensembl id to names map")
    gene_names_and_ids = get_gene_names_and_ensembl_and_entrez_ids()
    gene_ensembl_id_to_names = gene_names_and_ids.set_index("ensembl_gene_id")
    return gene_ensembl_id_to_names


def map_gene_ensembl_id_to_names(gid, gene_ensembl_id_to_names):
    """Map a gene Ensembl id to a gene name list.

    Parameters
    ----------
    gid : str
        Gene Ensembl id
    gene_ensembl_id_to_names : pd.DataFrame
        DataFrame indexed by gene Ensembl id containing gene name

    Returns
    -------
    list
        Gene names
    """
    if gid in gene_ensembl_id_to_names.index:
        names = gene_ensembl_id_to_names.loc[gid, "external_gene_name"]
        if isinstance(names, pd.core.series.Series):
            names = names.to_list()
        else:
            names = [names]
        # print(f"Mapped gene Ensembl id {gid} to names {names}")
    else:
        print(f"Could not find gene names for gene Ensembl id: {gid}")
        names = []
    return names


def get_gene_name_to_entrez_ids_map():
    """Get gene name to Entrez ids map.

    Parameters
    ----------
    None

    Returns
    -------
    gene_name_to_entrez_ids : pd.DataFrame
        DataFrame indexed by gene name containing gene Entrez id
    """
    print("Creating gene name to Entrez ids map")
    gene_names_and_ids = get_gene_names_and_ensembl_and_entrez_ids()
    gene_name_to_entrez_ids = gene_names_and_ids.set_index("external_gene_name")
    return gene_name_to_entrez_ids


def map_gene_name_to_entrez_ids(name, gene_name_to_entrez_ids):
    """Map a gene name to a gene Entrez id list.

    Parameters
    ----------
    name : str
        Gene name
    gene_name_to_entrez_ids : pd.DataFrame
        DataFrame indexed by gene name containing gene Entrez id

    Returns
    -------
    list
        Gene Entrez ids
    """
    if name in gene_name_to_entrez_ids.index:
        ids = gene_name_to_entrez_ids.loc[name, "entrezgene_id"]
        if isinstance(ids, pd.core.series.Series):
            ids = ids.to_list()
        else:
            ids = [ids]
        # print(f"Mapped gene name {name} to Entrez ids {ids}")
    else:
        print(f"Could not find gene Entrez ids for gene name: {name}")
        ids = []
    return ids


def get_gene_entrez_id_to_names_map():
    """Map gene Entrez id to names.

    Parameters
    ----------
    None

    Returns
    -------
    gene_entrez_id_to_names : pd.DataFrame
        DataFrame indexed by gene Entrez ids containing gene names
    """
    print("Creating gene Entrez id to names map")
    gene_names_and_ids = get_gene_names_and_ensembl_and_entrez_ids()
    gene_entrez_id_to_names = gene_names_and_ids.set_index("entrezgene_id")
    return gene_entrez_id_to_names


def map_gene_entrez_id_to_names(gid, gene_entrez_id_to_names):
    """Map a gene Entrez id to a gene name list.

    Parameters
    ----------
    gid : str
        Gene Entrez id
    gene_entrez_id_to_names : pd.DataFrame
        DataFrame indexed by gene Entrez id containing gene name

    Returns
    -------
    list
        Gene names
    """
    if gid in gene_entrez_id_to_names.index:
        names = gene_entrez_id_to_names.loc[gid, "external_gene_name"]
        if isinstance(names, pd.core.series.Series):
            names = names.to_list()
        else:
            names = [names]
        # print(f"Mapped gene Entrez id {gid} to names {names}")
    else:
        print(f"Could not find gene names for gene Entrez id: {gid}")
        names = []
    return names


def get_protein_ensembl_id_to_accession_map(protein_ids):
    """Map Ensembl protein ids to UniProt accession lists.

    Parameters
    ----------
    protein_ids : list(str)
        Protein ids returned by gget opentargets command

    Returns
    -------
    ensp2accn : dict
        Dictionary mapping Ensembl protein ids to UniProt accession
        lists
    """
    ensp2accn = {}

    # Submit Ensembl ids in batches to the UniProt id mapping service
    batch_size = 1000
    ensps = []
    for protein_id in protein_ids:
        if "ENSP" in protein_id:
            ensps.append(protein_id)

        if len(ensps) == batch_size or (
            len(ensps) > 0 and protein_id == protein_ids[-1]
        ):
            # Submit full, or the last batch
            job_id = submit_id_mapping(
                from_db="Ensembl_Protein", to_db="UniProtKB", ids=ensps
            )
            if check_id_mapping_results_ready(job_id):
                link = get_id_mapping_results_link(job_id)
                data = get_id_mapping_results_search(link)

            # Collect the mapping results
            for result in data["results"]:
                ensp = result["from"]
                accn = result["to"]["primaryAccession"]
                if ensp not in ensp2accn:
                    ensp2accn[ensp] = accn
                else:
                    if not isinstance(ensp2accn[ensp], list):
                        ensp2accn[ensp] = [ensp2accn[ensp]]
                    ensp2accn[ensp].append(accn)

            # Initialize for the next batch
            ensps = []

    return ensp2accn


def map_protein_ensembl_id_to_accession(ensp, ensp2accn):
    """Map Ensembl protein id to UniProt accession, selecting the
    first if more than one found.

    Parameters
    ----------
    ensp : str
        Ensembl protein id
    ensp2accn : dict
        Dictionary mapping Ensembl protein ids to UniProt accession
        lists

    Returns
    -------
    accn : str
        UniProt accession
    """
    accn = None

    if ensp in ensp2accn:
        accn = ensp2accn[ensp]
        if isinstance(accn, list):
            accn = accn[0]

    return accn


def get_protein_accession_to_ensembl_id_map(protein_ids):
    """Map UniProt accession to Ensembl protein ids lists.

    Parameters
    ----------
    protein_ids : list(str)
        Protein ids returned by gget opentargets command

    Returns
    -------
    accn2esnp : dict
        Dictionary mapping UniProt accession to Ensembl protein ids
        lists
    """
    accn2esnp = {}

    # Submit UniProt accessions in batches to the UniProt id mapping
    # service
    batch_size = 1000
    accns = []
    for protein_id in protein_ids:
        if "ENSP" not in protein_id:
            accns.append(protein_id)

        if len(accns) == batch_size or (
            len(accns) > 0 and protein_id == protein_ids[-1]
        ):
            # Submit full, or the last batch
            job_id = submit_id_mapping(
                from_db="UniProtKB_AC-ID", to_db="Ensembl_Protein", ids=accns
            )
            if check_id_mapping_results_ready(job_id):
                link = get_id_mapping_results_link(job_id)
                data = get_id_mapping_results_search(link)

            # Collect the mapping results
            for result in data["results"]:
                accn = result["from"]
                ensp = result["to"]
                if accn not in accn2esnp:
                    accn2esnp[accn] = ensp
                else:
                    if not isinstance(accn2esnp[accn], list):
                        accn2esnp[accn] = [accn2esnp[accn]]
                    accn2esnp[accn].append(ensp)

            # Initialize for the next batch
            accns = []

    return accn2esnp


def map_accession_to_protein_ensembl_id(accn, accn2ensp):
    """Map UniProt accession to Ensembl protein id, selecting the
    first if more than one found.

    Parameters
    ----------
    accn : str
        UniProt accession
    accn2esnp : dict
        Dictionary mapping UniProt accession to Ensembl protein ids
        lists

    Returns
    -------
    ensp : str
        Ensembl protein id
    """
    ensp = None

    if accn in accn2ensp:
        ensp = accn2ensp[accn]
        if isinstance(ensp, list):
            ensp = ensp[0]

    return ensp


def collect_unique_gene_names(nsforest_results):
    """Collect unique gene names found in the NSForest results marker
    or binary genes. Exclude clusters smaller than the minimum
    size. Return these values as a sorted list for restarting.

    Parameters
    ----------
    nsforest_results : pd.DataFrame
        DataFrame containing NSForest results

    Returns
    -------
    gene_names : list(str)
        List of unique gene names
    """
    gene_names = set()

    for column in spec.GENE_LIST_COLUMNS:
        for gene_list_str in nsforest_results.loc[
            nsforest_results["clusterSize"] >= MIN_CLUSTER_SIZE, column
        ]:
            gene_names |= set(ast.literal_eval(gene_list_str))

    return sorted(gene_names)


def collect_unique_gene_ensembl_ids(gene_names):
    """Collect unique Ensembl gene ids corresponding to the specified
    list of gene names. Return these values as a sorted list for
    restarting.

    Note that if gene names are taken from NSForest results, gene
    names might actually be Ensembl ids.

    Parameters
    ----------
    gene_names : list(str)
        List of gene names

    Returns
    -------
    gene_ensembl_ids : list(str)
        List of unique gene Ensembl ids
    """
    gene_ensembl_ids = set()

    gene_names = set(gene_names)
    gene_name_to_ensembl_ids = get_gene_name_to_ensembl_ids_map()
    for gene_name in gene_names:
        if "ENSG" in gene_name:
            gene_ensembl_id = gene_name.split(".")[0]
        else:
            _gene_ensembl_ids = map_gene_name_to_ensembl_ids(
                gene_name, gene_name_to_ensembl_ids
            )
            if len(_gene_ensembl_ids) == 0:
                gene_ensembl_id = None
            else:
                gene_ensembl_id = _gene_ensembl_ids[0]
        if gene_ensembl_id:
            gene_ensembl_ids.add(gene_ensembl_id)
    print(
        f"Collected {len(gene_ensembl_ids)} unique Ensembl gene ids for {len(gene_names)} unique gene names"
    )

    return sorted(gene_ensembl_ids)


def collect_unique_gene_entrez_ids(gene_names):
    """Collect unique Entrez gene ids corresponding to the specified
    list of gene names. Return these values as a sorted list for
    restarting.

    Note that if gene names are taken from NSForest results, gene
    names might actually be Ensembl ids.

    Parameters
    ----------
    gene_names : list(str)
        List of gene names

    Returns
    -------
    gene_ids : list(str)
        List of unique gene Entrez ids
    """
    gene_entrez_ids = set()

    gene_names = set(gene_names)
    gene_ensembl_id_to_names = get_gene_ensembl_id_to_names_map()
    gene_name_to_entrez_ids = get_gene_name_to_entrez_ids_map()
    for gene_name in gene_names:
        if "ENSG" in gene_name:
            gene_ensembl_id = gene_name.split(".")[0]
            _gene_names = map_gene_ensembl_id_to_names(
                gene_ensembl_id, gene_ensembl_id_to_names
            )
            if len(_gene_names) == 0:
                gene_name = None
            else:
                gene_name = _gene_names[0]
        _gene_entrez_ids = map_gene_name_to_entrez_ids(
            gene_name, gene_name_to_entrez_ids
        )
        if len(_gene_entrez_ids) == 0:
            gene_entrez_id = None
        else:
            gene_entrez_id = _gene_entrez_ids[0]
        if gene_entrez_id:
            gene_entrez_ids.add(gene_entrez_id)
    print(
        f"Collected {len(gene_entrez_ids)} unique Entrez gene ids for {len(gene_names)} unique gene names"
    )

    return sorted(gene_entrez_ids)


def get_efo_to_mondo_map():
    """Get EFO to MONDO term map.

    Parameters
    ----------
    None

    Returns
    -------
    efo2mondo : pd.DataFrame
        DataFrame indexed by EFO containing MONDO term
    """
    print("Creating EFO to MONDO term map")
    mondo_efo_mappings_name = (
        Path(__file__).parents[2] / "data" / "mondo_efo_mappings.csv"
    )
    efo2mondo = pd.read_csv(mondo_efo_mappings_name)
    efo2mondo = efo2mondo.set_index("EFO")
    return efo2mondo


def map_efo_to_mondo(efo, efo2mondo):
    """Map EFO to MONDO term.

    Parameters
    ----------
    efo : str
        EFO term
    efo2mondo : pd.DataFrame
        DataFrame indexed by EFO containing MONDO term

    Returns
    -------
    str
        MONDO term
    """
    if efo in efo2mondo.index:
        mondo = efo2mondo.loc[efo, "MONDO"]
    else:
        # print(f"Could not find MONDO for EFO term: {efo}")
        return None
    return mondo


def get_mesh_to_mondo_map(obo_dir, obo_fnm):
    """Parse MONDO ontology XML downloaded from the OBO Foundry to
    create a mapping from MeSH term to MONDO term.

    Parameters
    ----------
    obo_dir : str | Path
        Name of directory containing downloaded MONDO ontology XML
    obo_fnm : str
        Name of downloaded MONDO ontology XML file

    Returns
    -------
    mesh2mondo : dict
        Dictionary mapping MeSH term to MONDO term
    """
    mesh2mondo = {}
    root = etree.parse(Path(obo_dir) / obo_fnm)
    for class_element in root.iter(f"{OWL_NS}Class"):
        # Look for an about attribute
        uriref = class_element.get(f"{RDF_NS}about")
        if uriref is None:
            continue

        id, number, mondo_term, _, _ = parse_term(uriref)
        if id is None:
            continue

        for hasDbXref_element in class_element.iter(f"{OBO_IN_OWL_NS}hasDbXref"):
            if hasDbXref_element is None:
                continue
            mesh_term = hasDbXref_element.text
            if "MESH" in mesh_term:
                mesh2mondo[mesh_term] = mondo_term
                break

    # https://meshb.nlm.nih.gov/record/ui?ui=D000077192
    # http://purl.obolibrary.org/obo/MONDO_0004991
    mesh2mondo["MESH:D000077192"] = "MONDO_0004991"

    # https://meshb.nlm.nih.gov/record/ui?ui=D000086382
    # http://purl.obolibrary.org/obo/MONDO_0100096
    mesh2mondo["MESH:D000086382"] = "MONDO_0100096"

    # https://meshb.nlm.nih.gov/record/ui?ui=D003643
    # http://purl.obolibrary.org/obo/UBERON_0000071
    mesh2mondo["MESH:D003643"] = "UBERON_0000071"

    # https://meshb.nlm.nih.gov/record/ui?ui=D005355
    # http://purl.obolibrary.org/obo/MONDO_0002771
    mesh2mondo["MESH:D005355"] = "MONDO_0002771"

    return mesh2mondo


def map_mesh_to_mondo(mesh, mesh2mondo):
    """Map MeSH term to MONDO term.

    Parameters
    ----------
    mesh : str
        MeSH term
    mesh2mondo : dict
        Dictionary mapping MeSH term to MONDO term

    Returns
    -------
    mondo : str
        MONDO term
    """
    mondo = None

    if mesh in mesh2mondo:
        mondo = mesh2mondo[mesh]

    return mondo


def get_chembl_to_pubchem_map():
    """Get ChEMBL to PubChem id map.

    Parameters
    ----------
    None

    Returns
    -------
    chembl2pubchem : pd.DataFrame
        DataFrame indexed by ChEMBL id containing PubChem id
    """
    print("Creating ChEMBL to PubChem id map")
    src1src22_path = Path(__file__).parents[2] / "data" / "src1src22.csv"
    chembl2pubchem = pd.read_csv(src1src22_path)
    chembl2pubchem = chembl2pubchem.set_index("ChEMBL")
    return chembl2pubchem


def map_chembl_to_pubchem(chembl, chembl2pubchem):
    """Map ChEMBL to PubChem id.

    Parameters
    ----------
    chembl : str
        ChEMLB id
    chembl2pubchem : pd.DataFrame
        DataFrame indexed by ChEMBL containing PubChem id

    Returns
    -------
    str
        PubChem id
    """
    pubchem = None

    if chembl in chembl2pubchem.index:
        pubchem = chembl2pubchem.loc[chembl, "PubChem"]
        if isinstance(pubchem, pd.core.series.Series):
            pubchem = pubchem.iloc[0]

    return pubchem


def get_value_or_none(data, keys):
    """Return the value in the data corresponding to the last key, or
    None, if any key is not in the data.

    Parameters
    ----------
    data : dict
        Dictionary which may or may not contain the keys
    keys : list(str)
        List of keys to access the dictionary in order
    """
    value = None
    for key in keys:
        try:
            if value is None:
                value = data[key]
            else:
                value = value[key]
        except (KeyError, TypeError):
            return None
    return value


def get_values_or_none(data, list_key, value_keys):
    """Collect and return the values for each list item in the data
    corresponding to the list key, and last value key.

    Parameters
    ----------
    data : dict
        Dictionary which may or may not contain the keys
    list_key : str
        Key of the list of items
    value_keys : list(str)
        List of keys to access each item in order
    """
    values = ""
    if list_key in data:
        for item in data[list_key]:
            value = get_value_or_none(item, value_keys)
            if values == "":
                values = value
            else:
                values += ", " + value
    return values


def main():
    import sys

    # Pass None when no path is given so each callee resolves via get_current_run().
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    harvester_data = get_cellxgene_harvester_data(results_dir)

    file_paths = get_dataset_file_paths(results_dir)

    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)

    return harvester_data, file_paths, dataset_version_id_lists


if __name__ == "__main__":
    import JsonErrors

    JsonErrors.install()
    harvester_data, file_paths, dataset_version_id_lists = main()
