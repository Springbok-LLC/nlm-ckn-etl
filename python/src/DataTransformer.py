import argparse
import base64
import gzip
import json
import re
from datetime import datetime, timezone
from time import monotonic

import structlog

from E_Utilities import parse_xml_for_gene_id

from LoaderUtilities import (
    OPENTARGETS_RESOURCES,
    build_citation,
    get_current_run,
    get_value_or_none,
    get_values_or_none,
    set_current_run,
)
from logging_setup import configure_logging

_events = structlog.get_logger(__name__)

# Rejected ids logged on a ``records_rejected`` line, so a large reject does
# not become a large log line.
_REJECT_SAMPLE_SIZE = 5


def _ms(started: float) -> int:
    """Return the milliseconds elapsed since a ``time.monotonic()`` reading."""
    return round((monotonic() - started) * 1000)


def _mtime_iso(path) -> str:
    """Return a file's modification time as an ISO 8601 UTC string."""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


class BaseTransformer:
    """Base class for transforming raw fetcher output into the shape
    expected by downstream tuple writers. Subclasses implement
    transform()."""

    name: str

    _input_path_override = None

    def _reject(self, reason, id_value):
        """Record an input entry the transformer dropped, and why.

        Parameters
        ----------
        reason : str
            A ``reason`` value from ``docs/logging-conventions.md``
        id_value : str
            The entry's id
        """
        if "_rejects" not in self.__dict__:
            self._rejects = {}
        self._rejects.setdefault(reason, []).append(id_value)

    @property
    def input_path(self):
        """Path to raw JSON from the corresponding fetcher. Defaults to
        ``<run external_dir>/<name>.json`` but may be overridden by
        assignment (e.g. from tests)."""
        if self._input_path_override is not None:
            return self._input_path_override
        return get_current_run().external_dir / f"{self.name}.json"

    @input_path.setter
    def input_path(self, value):
        self._input_path_override = value

    @property
    def output_path(self):
        """Path where transformed results are saved."""
        return self.input_path.parent / f"{self.name}_transformed.json"

    def is_stale(self):
        """Check whether the transformer needs to run.

        Returns
        -------
        bool
            True if the output is missing or older than the input
        """
        if not self.output_path.exists():
            return True
        return self.input_path.stat().st_mtime > self.output_path.stat().st_mtime

    def load(self):
        """Load raw results from disk.

        Returns
        -------
        dict
            Raw results as saved by the corresponding fetcher
        """
        with open(self.input_path, "r") as fp:
            return json.load(fp)

    def save(self, results):
        """Save transformed results to disk.

        Parameters
        ----------
        results : dict
            Transformed results to persist
        """
        with open(self.output_path, "w") as fp:
            json.dump(results, fp)

    def transform(self, raw_results):
        """Transform raw API results into the shape expected by tuple
        writers.

        Parameters
        ----------
        raw_results : dict
            Raw results as returned by the corresponding fetcher

        Returns
        -------
        dict
            Transformed results
        """
        raise NotImplementedError

    def run(self, force=False):
        """Load raw results, transform, and save them.

        Parameters
        ----------
        force : bool
            If True, run even when the output is up to date

        Returns
        -------
        dict
            Transformed results
        """
        started = monotonic()
        if not force and not self.is_stale():
            _events.info(
                "transform_skipped_up_to_date",
                source=self.name,
                input_mtime=_mtime_iso(self.input_path),
                output_mtime=_mtime_iso(self.output_path),
            )
            with open(self.output_path, "r") as fp:
                return json.load(fp)
        self._rejects = {}
        raw = self.load()
        results = self.transform(raw)
        self.save(results)
        self._log_finished(raw, results, started)
        return results

    def _log_finished(self, raw, results, started):
        """Log the rejected entries and ``transform_finished``.

        The entries are the dict-valued items: the id lists a fetcher stores
        beside its results are not records.  An entry the transformer left
        empty without a recorded reason is counted as ``no_data``.

        Parameters
        ----------
        raw : dict
            The raw results the transformer read
        results : dict
            The transformed results
        started : float
            ``time.monotonic()`` reading from the start of the run
        """
        records_in = sum(isinstance(v, dict) for v in raw.values())
        records_out = sum(1 for v in results.values() if isinstance(v, dict) and v)
        rejects = dict(self._rejects)
        unexplained = records_in - records_out - sum(len(v) for v in rejects.values())
        if unexplained > 0:
            rejects["no_data"] = [None] * unexplained
        for reason, ids in rejects.items():
            _events.warning(
                "records_rejected",
                source=self.name,
                reason=reason,
                records_rejected=len(ids),
                sample=[i for i in ids if i is not None][:_REJECT_SAMPLE_SIZE],
            )
        _events.info(
            "transform_finished",
            source=self.name,
            input_path=str(self.input_path),
            output_path=str(self.output_path),
            records_in=records_in,
            records_out=records_out,
            records_rejected=records_in - records_out,
            duration_ms=_ms(started),
        )


def _publication_in(citation):
    """Return the publication link in a CELLxGENE citation string, or None."""
    if not citation:
        return None
    m = re.search(r"Publication:\s*(\S*)\s*Dataset Version:", citation)
    return m.group(1) if m and m.group(1) else None


class CellxGeneTransformer(BaseTransformer):
    """Extracts Citation, links, tissue, organism, and other metadata
    fields from raw CELLxGENE dataset and collection JSON."""

    name = "cellxgene"

    def transform(self, raw_results):
        """Transform raw CELLxGENE results.

        Parameters
        ----------
        raw_results : dict
            Keyed by dataset_version_id, each value has
            'dataset_json' and 'collection_json', and, once fetched,
            'collection_version_json'

        Returns
        -------
        dict
            Keyed by dataset_version_id with extracted metadata fields
        """
        transformed = {}
        for dataset_version_id, raw in raw_results.items():
            dataset_json = raw.get("dataset_json", {})
            collection_json = raw.get("collection_json")
            # The collection as of the version the dataset version belongs to.
            # The current collection can describe a later publication (a
            # preprint replaced by its journal paper), so the publication
            # fields come from the version, and from the current collection
            # only for a cache entry fetched before the version was kept.
            collection_version_json = raw.get("collection_version_json")
            publication_json = collection_version_json or collection_json

            if not dataset_json or not publication_json:
                self._reject("missing_dataset_or_collection", dataset_version_id)
                continue

            entry = {}

            # Publication metadata
            pub_meta = publication_json.get("publisher_metadata", {})
            authors = pub_meta.get("authors", [])
            first_author = authors[0]["family"] if authors else None
            published_year = pub_meta.get("published_year")
            journal = pub_meta.get("journal")
            title = publication_json.get("name")

            entry["Citation"] = build_citation(first_author, published_year, journal)
            # A consortium author carries a single "name" rather than a
            # family and given name. Join only the parts that are present,
            # so such an author reads as its name instead of ", , ".
            author_names = []
            for a in authors:
                parts = [
                    part.strip()
                    for part in (a.get("family"), a.get("given"))
                    if part and part.strip()
                ]
                name = ", ".join(parts) or (a.get("name") or "").strip()
                if name:
                    author_names.append(name)
            entry["Author_list"] = ", ".join(author_names) if author_names else None
            entry["Year"] = str(published_year) if published_year else None
            entry["Title"] = title
            entry["Journal"] = journal

            # Publication and collection links. The dataset version's own
            # citation names the publication it was published with, so it
            # comes first; the current collection's doi describes the latest
            # publication, which can be a later one. The doi of the collection
            # version, then the current collection's citation and doi, are
            # the fallbacks, and the only source for collections whose
            # citation string is absent or carries no publication (e.g.
            # Jorstad (2023) Science).
            current = collection_json or {}
            version = collection_version_json or {}
            dataset_citation = dataset_json.get("citation")
            collection_citation = current.get("citation")
            version_doi = version.get("doi")
            current_doi = current.get("doi")
            link = _publication_in(dataset_citation)
            if not link and version_doi:
                link = f"https://doi.org/{version_doi}"
            if not link:
                link = _publication_in(collection_citation)
            if not link and current_doi:
                link = f"https://doi.org/{current_doi}"
            entry["Link_to_publication"] = link
            entry["Link_to_CELLxGENE_collection"] = None
            citation = dataset_citation or collection_citation
            if citation:
                m = re.search(r"Collection:\s*(\S*)$", citation)
                if m:
                    entry["Link_to_CELLxGENE_collection"] = m.group(1)

            # Dataset metadata. Prefer the H5AD asset: a dataset can list other
            # assay files first (e.g. ATAC fragments), and assets[0] is not
            # guaranteed to be the h5ad (see NIH-NLM/nlm-ckn#342). assets[0]
            # is used only as a compatibility fallback for a response with no
            # filetype tag at all; when assets are tagged but none is H5AD
            # (e.g. an ATAC-only dataset), the link is left unset rather than
            # silently pointing at the wrong assay's file.
            assets = dataset_json.get("assets", [])
            h5ad_asset = next((a for a in assets if a.get("filetype") == "H5AD"), None)
            if h5ad_asset is not None:
                entry["Link_to_CELLxGENE_dataset"] = h5ad_asset["url"]
            elif any(a.get("filetype") for a in assets):
                entry["Link_to_CELLxGENE_dataset"] = None
                _events.warning(
                    "no_h5ad_asset",
                    dataset_version_id=dataset_version_id,
                    filetypes=[a.get("filetype") for a in assets],
                )
            else:
                entry["Link_to_CELLxGENE_dataset"] = get_value_or_none(
                    dataset_json, ["assets", 0, "url"]
                )
            entry["Dataset_name"] = get_value_or_none(dataset_json, ["title"])
            entry["Number_of_cells"] = get_value_or_none(dataset_json, ["cell_count"])
            entry["Organism"] = get_values_or_none(dataset_json, "organism", ["label"])
            entry["Tissue"] = get_values_or_none(dataset_json, "tissue", ["label"])
            entry["Disease_status"] = get_values_or_none(
                dataset_json, "disease", ["label"]
            )

            # IDs
            entry["Collection_ID"] = get_value_or_none(dataset_json, ["collection_id"])
            entry["Collection_version_ID"] = get_value_or_none(
                dataset_json, ["collection_version_id"]
            )
            entry["Dataset_ID"] = get_value_or_none(dataset_json, ["dataset_id"])
            entry["Dataset_version_ID"] = dataset_version_id

            transformed[dataset_version_id] = entry

        return transformed


# Maps resource name to the GraphQL response path
_RESOURCE_PATHS = {
    "diseases": ("associatedDiseases", "rows"),
    "drugs": ("drugAndClinicalCandidates", "rows"),
    "pharmacogenetics": ("pharmacogenomics", None),
}


class OpenTargetsTransformer(BaseTransformer):
    """Maps nested GraphQL response to flat resource keys (diseases,
    drugs, pharmacogenetics)."""

    name = "opentargets"

    def transform(self, raw_results):
        """Transform raw Open Targets results.

        Parameters
        ----------
        raw_results : dict
            Keyed by gene_ensembl_id, each value is the raw GraphQL
            'data' dict

        Returns
        -------
        dict
            Keyed by gene_ensembl_id with 'target' info and flat
            resource keys
        """
        transformed = {}
        for gene_ensembl_id, data in raw_results.items():
            if gene_ensembl_id == "gene_ensembl_ids":
                transformed[gene_ensembl_id] = data
                continue

            entry = {}

            target = data.get("target", {})
            if target:
                entry["target"] = {}
                for key in [
                    "id",
                    "dbXrefs",
                    "proteinIds",
                    "transcriptIds",
                    "approvedSymbol",
                    "approvedName",
                ]:
                    entry["target"][key] = target.get(key)

                for resource in OPENTARGETS_RESOURCES:
                    gql_key, rows_key = _RESOURCE_PATHS[resource]
                    resource_data = target.get(gql_key, {})
                    if rows_key and isinstance(resource_data, dict):
                        resource_data = resource_data.get(rows_key, [])
                    entry[resource] = resource_data
            else:
                entry["target"] = {}
                for resource in OPENTARGETS_RESOURCES:
                    entry[resource] = {}

            transformed[gene_ensembl_id] = entry

        return transformed


class GeneTransformer(BaseTransformer):
    """Decompresses stored XML and extracts gene data fields using
    E_Utilities parsing."""

    name = "gene"

    def transform(self, raw_results):
        """Decompress and parse stored XML for each gene.

        Parameters
        ----------
        raw_results : dict
            Keyed by gene_entrez_id, each value has 'xml_gz_b64'
            containing compressed XML

        Returns
        -------
        dict
            Keyed by gene_entrez_id with extracted gene data fields
        """
        transformed = {}
        for gene_entrez_id, raw in raw_results.items():
            if gene_entrez_id == "gene_entrez_ids":
                transformed[gene_entrez_id] = raw
                continue

            if not raw or "xml_gz_b64" not in raw:
                transformed[gene_entrez_id] = {}
                continue

            try:
                xml_data = gzip.decompress(base64.b64decode(raw["xml_gz_b64"])).decode(
                    "utf-8"
                )
                transformed[gene_entrez_id] = parse_xml_for_gene_id(
                    gene_entrez_id, xml_data
                )
            except Exception as exc:
                _events.warning(
                    "gene_parse_failed",
                    gene_id=gene_entrez_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self._reject("parse_failed", gene_entrez_id)
                transformed[gene_entrez_id] = {}

        return transformed


class UniProtTransformer(BaseTransformer):
    """Extracts Protein_name, Gene_name, Function, and other fields
    from raw UniProt API responses."""

    name = "uniprot"

    def transform(self, raw_results):
        """Transform raw UniProt results.

        Parameters
        ----------
        raw_results : dict
            Keyed by protein_accession, each value is the raw UniProt
            API response

        Returns
        -------
        dict
            Keyed by protein_accession with extracted fields
        """
        transformed = {}
        for accession, response_json in raw_results.items():
            if accession == "protein_accessions":
                transformed[accession] = response_json
                continue

            if not response_json:
                transformed[accession] = {}
                continue

            data = {}
            data["Protein_name"] = get_value_or_none(
                response_json,
                [
                    "proteinDescription",
                    "recommendedName",
                    "fullName",
                    "value",
                ],
            )
            data["UniProt_ID"] = get_value_or_none(response_json, ["primaryAccession"])
            data["Gene_name"] = None
            if "genes" in response_json and len(response_json["genes"]) > 0:
                data["Gene_name"] = get_value_or_none(
                    response_json["genes"][0],
                    [
                        "geneName",
                        "value",
                    ],
                )
            data["Number_of_amino_acids"] = get_value_or_none(
                response_json,
                [
                    "sequence",
                    "length",
                ],
            )
            data["Function"] = None
            if "comments" in response_json:
                for comment in response_json["comments"]:
                    if (
                        "commentType" in comment
                        and comment["commentType"] == "FUNCTION"
                    ):
                        if "texts" in comment and len(comment["texts"]) > 0:
                            data["Function"] = get_value_or_none(
                                comment["texts"][0], ["value"]
                            )
            data["Annotation_score"] = get_value_or_none(
                response_json, ["annotationScore"]
            )
            data["Organism"] = get_value_or_none(
                response_json,
                [
                    "organism",
                    "scientificName",
                ],
            )
            transformed[accession] = data

        return transformed


TRANSFORMER_REGISTRY = [
    CellxGeneTransformer(),
    OpenTargetsTransformer(),
    GeneTransformer(),
    UniProtTransformer(),
]


def main():
    """Transform raw fetcher output for all registered sources.

    Logs the run as ``run_started`` and ``run_finished``, or ``run_failed``.
    """
    configure_logging("etl-fetcher")
    started = monotonic()
    try:
        _transform_all()
    except Exception as exc:
        _events.error(
            "run_failed",
            script="DataTransformer",
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=_ms(started),
        )
        raise
    _events.info(
        "run_finished",
        script="DataTransformer",
        outcome="ok",
        duration_ms=_ms(started),
    )


def _transform_all():
    """Parse the command line and transform each requested source."""
    parser = argparse.ArgumentParser(description="Transform External API Results")
    parser.add_argument(
        "sources",
        nargs="*",
        help="source names to transform (default: all)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run transformers even if output is up to date",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="run name (selects data/results-<name>/; defaults to $CKN_RUN or 'full')",
    )
    args = parser.parse_args()

    set_current_run(args.run_name)

    source_names = args.sources or [t.name for t in TRANSFORMER_REGISTRY]
    _events.info(
        "run_started",
        script="DataTransformer",
        run_name=get_current_run().run_name,
        sources=source_names,
        force=args.force,
    )

    for transformer in TRANSFORMER_REGISTRY:
        if transformer.name in source_names:
            transformer.run(force=args.force)


if __name__ == "__main__":
    main()
