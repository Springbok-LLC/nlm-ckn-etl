import argparse
import base64
import gzip
import json
import re

from E_Utilities import parse_xml_for_gene_id

from LoaderUtilities import (
    OPENTARGETS_RESOURCES,
    get_current_run,
    get_value_or_none,
    get_values_or_none,
    set_current_run,
)


class BaseTransformer:
    """Base class for transforming raw fetcher output into the shape
    expected by downstream tuple writers. Subclasses implement
    transform()."""

    name: str

    _input_path_override = None

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
        print(f"[{self.name}] Loading raw results from {self.input_path}")
        with open(self.input_path, "r") as fp:
            return json.load(fp)

    def save(self, results):
        """Save transformed results to disk.

        Parameters
        ----------
        results : dict
            Transformed results to persist
        """
        print(f"[{self.name}] Saving transformed results to {self.output_path}")
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
        if not force and not self.is_stale():
            print(f"[{self.name}] Output is up to date, skipping")
            with open(self.output_path, "r") as fp:
                return json.load(fp)
        raw = self.load()
        results = self.transform(raw)
        self.save(results)
        return results


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
            'dataset_json' and 'collection_json'

        Returns
        -------
        dict
            Keyed by dataset_version_id with extracted metadata fields
        """
        transformed = {}
        for dataset_version_id, raw in raw_results.items():
            dataset_json = raw.get("dataset_json", {})
            collection_json = raw.get("collection_json")

            if not dataset_json or not collection_json:
                print(
                    f"[{self.name}] Skipping dataset_version_id "
                    f"{dataset_version_id} (missing data)"
                )
                continue

            entry = {}

            # Publication metadata
            pub_meta = collection_json.get("publisher_metadata", {})
            authors = pub_meta.get("authors", [])
            first_author = authors[0]["family"] if authors else None
            published_year = pub_meta.get("published_year")
            journal = pub_meta.get("journal")
            title = collection_json.get("name")

            entry["Citation"] = f"{first_author} ({published_year}) {journal}"
            entry["Author_list"] = (
                ", ".join(
                    f"{a.get('family', '')}, {a.get('given', '')}"
                    for a in authors
                )
                if authors
                else None
            )
            entry["Year"] = str(published_year) if published_year else None
            entry["Title"] = title
            entry["Journal"] = journal

            # Publication and collection links
            entry["Link_to_publication"] = None
            entry["Link_to_CELLxGENE_collection"] = None
            citation = get_value_or_none(dataset_json, ["citation"])
            if not citation:
                citation = get_value_or_none(collection_json, ["citation"])
            if citation:
                m = re.search(r"Publication:\s*(\S*)\s*Dataset Version:", citation)
                if m:
                    entry["Link_to_publication"] = m.group(1)
                m = re.search(r"Collection:\s*(\S*)$", citation)
                if m:
                    entry["Link_to_CELLxGENE_collection"] = m.group(1)

            # Dataset metadata
            entry["Link_to_CELLxGENE_dataset"] = dataset_json["assets"][0]["url"]
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
    "interactions": ("interactions", "rows"),
    "pharmacogenetics": ("pharmacogenomics", None),
    "tractability": ("tractability", None),
    "expression": ("expressions", None),
    "depmap": ("depMapEssentiality", None),
}


class OpenTargetsTransformer(BaseTransformer):
    """Maps nested GraphQL response to flat resource keys (diseases,
    drugs, interactions, etc.)."""

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

            xml_data = gzip.decompress(
                base64.b64decode(raw["xml_gz_b64"])
            ).decode("utf-8")
            transformed[gene_entrez_id] = parse_xml_for_gene_id(
                gene_entrez_id, xml_data
            )

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
    """Transform raw fetcher output for all registered sources."""
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
        "--run",
        default=None,
        help="run name (selects data/run-<name>.json; "
        "defaults to $CKN_RUN or 'full')",
    )
    args = parser.parse_args()

    set_current_run(args.run)

    source_names = args.sources or [t.name for t in TRANSFORMER_REGISTRY]

    for transformer in TRANSFORMER_REGISTRY:
        if transformer.name in source_names:
            result = transformer.run(force=args.force)
            print(f"[{transformer.name}] Transformed {len(result)} entries")


if __name__ == "__main__":
    main()
