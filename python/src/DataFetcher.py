import argparse
import json
import os
from glob import glob
from pathlib import Path
import re
import shutil

import requests

import base64
import gzip

from E_Utilities import fetch_xml_for_gene_id
from OpenTargetsGGetQueries import gget_queries
from LoaderUtilities import (
    EXTERNAL_DIRPATH,
    OPENTARGETS_RESOURCES,
    get_cellxgene_harvester_data,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_results_sources,
    get_unique_gene_names_and_ids,
)


REQUEST_TIMEOUT = 30  # seconds

class DataFetcher:
    """Base class for all external API data fetchers. Subclasses implement
    get_ids() and fetch_one(). The batch loop, checkpointing, and CLI
    integration are handled here."""

    name: str
    output_path: Path
    batch_size: int = 25

    def get_ids(self, context):
        """Return the list of IDs to iterate over.

        Parameters
        ----------
        context : dict
            Shared pipeline state (gene_data, file_paths, etc.)

        Returns
        -------
        list
            IDs to fetch
        """
        raise NotImplementedError

    def fetch_one(self, id_value):
        """Fetch raw API data for a single ID.

        Parameters
        ----------
        id_value : str
            The identifier to fetch

        Returns
        -------
        dict
            Raw API response data
        """
        raise NotImplementedError

    def on_fetch_error(self, id_value):
        """Return a fallback value when fetch_one raises.

        Parameters
        ----------
        id_value : str
            The identifier that failed

        Returns
        -------
        dict
            Fallback value (default: empty dict)
        """
        return {}

    def before_dump(self, results, ids):
        """Hook called before each batch dump. Subclasses can store
        metadata like results['gene_entrez_ids'] = ids.

        Parameters
        ----------
        results : dict
            The results dict about to be saved
        ids : list
            The full list of IDs being processed
        """
        pass

    def run(self, context, force=False):
        """Execute the fetch loop with batch-save checkpointing.

        Parameters
        ----------
        context : dict
            Shared pipeline state
        force : bool
            Flag to force re-fetching

        Returns
        -------
        dict
            The full results dict
        """
        if not self.output_path.exists() or force:
            results = {}
        else:
            results = self._load()

        ids = self.get_ids(context)
        total_size = len(ids)
        n_so_far = 0
        do_dump = False
        n_in_batch = 0

        for id_value in ids:
            n_so_far += 1

            if id_value not in results:
                n_in_batch += 1
                print(
                    f"[{self.name}] Fetched {n_in_batch}/{self.batch_size} in batch"
                    f" - {n_so_far}/{total_size} so far"
                )
                do_dump = True

                try:
                    results[id_value] = self.fetch_one(id_value)
                except Exception as exc:
                    print(f"[{self.name}] Error fetching {id_value}: {exc}")
                    results[id_value] = self.on_fetch_error(id_value)

            else:
                if id_value != ids[-1]:
                    continue

            if do_dump and (n_in_batch >= self.batch_size or id_value == ids[-1]):
                do_dump = False
                n_in_batch = 0
                self.before_dump(results, ids)
                self._save(results)

        return results

    def _load(self):
        """Load results from the output path."""
        print(f"[{self.name}] Loading results from {self.output_path}")
        with open(self.output_path, "r") as fp:
            return json.load(fp)

    def _save(self, results):
        """Save results to the output path."""
        print(f"[{self.name}] Dumping results to {self.output_path}")
        with open(self.output_path, "w") as fp:
            json.dump(results, fp, indent=4)


class CellxGeneFetcher(DataFetcher):
    """Fetches dataset and collection metadata from the CELLxGENE
    curation API."""

    name = "cellxgene"
    output_path = EXTERNAL_DIRPATH / "cellxgene.json"

    BASE_URL = "https://api.cellxgene.cziscience.com/curation/v1"

    def get_ids(self, context):
        """Flatten dataset_version_id_lists into a single list."""
        if "dataset_version_id_lists" not in context:
            raise ValueError(
                "CellxGeneFetcher requires 'dataset_version_id_lists' in context"
            )
        dataset_version_id_lists = context["dataset_version_id_lists"]
        ids = []
        for id_list in dataset_version_id_lists:
            ids.extend(id_list)
        return ids

    def fetch_one(self, dataset_version_id):
        """Fetch dataset and collection JSON for a dataset version ID.

        Parameters
        ----------
        dataset_version_id : str
            CELLxGENE dataset version identifier

        Returns
        -------
        dict
            Raw response with keys 'dataset_json' and 'collection_json'
        """
        dataset_url = f"{self.BASE_URL}/dataset_versions/{dataset_version_id}"
        response = requests.get(dataset_url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(
                f"[{self.name}] Could not fetch dataset for "
                f"dataset_version_id {dataset_version_id}"
            )
            return {}

        dataset_json = response.json()

        collection_json = None
        collection_id = dataset_json.get("collection_id")
        if collection_id:
            collection_url = f"{self.BASE_URL}/collections/{collection_id}"
            resp = requests.get(collection_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                collection_json = resp.json()

        return {
            "dataset_json": dataset_json,
            "collection_json": collection_json,
        }


OPENTARGETS_BASE_URL = "https://api.platform.opentargets.org/api/v4/graphql"
class OpenTargetsFetcher(DataFetcher):
    """Fetches target and resource data from the Open Targets Platform
    GraphQL API."""

    name = "opentargets"
    output_path = EXTERNAL_DIRPATH / "opentargets.json"

    def get_ids(self, context):
        """Return gene Ensembl IDs."""
        if "gene_data" not in context:
            raise ValueError(
                "OpenTargetsFetcher requires 'gene_data' in context"
            )
        return context["gene_data"]["gene_ensembl_ids"]

    def fetch_one(self, gene_ensembl_id):
        """Fetch raw GraphQL response for a gene Ensembl ID.

        Parameters
        ----------
        gene_ensembl_id : str
            Ensembl gene identifier

        Returns
        -------
        dict
            Raw GraphQL 'data' dict
        """
        query = gget_queries["target"]
        query["variables"]["ensemblId"] = gene_ensembl_id
        response = requests.post(
            OPENTARGETS_BASE_URL,
            json={
                "query": query["query_string"],
                "variables": query["variables"],
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        print(
            f"[{self.name}] Assigning resources for gene Ensembl id {gene_ensembl_id}"
        )
        return json.loads(response.text)["data"]

    def on_fetch_error(self, gene_ensembl_id):
        """Return empty target and empty resources on error."""
        print(
            f"[{self.name}] Could not assign resources for "
            f"gene Ensembl id {gene_ensembl_id}"
        )
        result = {"target": {}}
        for resource in OPENTARGETS_RESOURCES:
            result[resource] = {}
        return result

    def before_dump(self, results, ids):
        """Store the gene Ensembl ID list in the results."""
        results["gene_ensembl_ids"] = ids


class GeneFetcher(DataFetcher):
    """Fetches gene data from NCBI Gene via E-Utilities."""

    name = "gene"
    output_path = EXTERNAL_DIRPATH / "gene.json"

    def get_ids(self, context):
        """Return gene Entrez IDs."""
        if "gene_data" not in context:
            raise ValueError(
                "GeneFetcher requires 'gene_data' in context"
            )
        return context["gene_data"]["gene_entrez_ids"]

    def fetch_one(self, gene_entrez_id):
        """Fetch raw XML for a gene Entrez ID, compress, and base64
        encode for JSON storage.

        Parameters
        ----------
        gene_entrez_id : str
            NCBI Gene Entrez identifier

        Returns
        -------
        dict
            Dict with 'xml_gz_b64' key containing compressed XML
        """
        print(
            f"[{self.name}] Fetching gene XML for "
            f"gene Entrez id {gene_entrez_id}"
        )
        xml_data = fetch_xml_for_gene_id(gene_entrez_id)
        if xml_data is None:
            return {}
        compressed = gzip.compress(xml_data.encode("utf-8"))
        return {"xml_gz_b64": base64.b64encode(compressed).decode("ascii")}

    def on_fetch_error(self, gene_entrez_id):
        """Return empty dict on error."""
        print(
            f"[{self.name}] Could not assign gene data for "
            f"gene Entrez id {gene_entrez_id}"
        )
        return {}

    def before_dump(self, results, ids):
        """Store the gene Entrez ID list in the results."""
        results["gene_entrez_ids"] = ids


class UniProtFetcher(DataFetcher):
    """Fetches protein data from the UniProt REST API."""

    name = "uniprot"
    output_path = EXTERNAL_DIRPATH / "uniprot.json"

    def get_ids(self, context):
        """Derive unique protein accessions from gene results.

        Parameters
        ----------
        context : dict
            Must contain 'gene_results' from a prior GeneFetcher run

        Returns
        -------
        list
            Unique protein accession strings
        """
        if "gene_results" not in context:
            raise ValueError(
                "UniProtFetcher requires 'gene_results' in context"
                " — run GeneFetcher first"
            )
        gene_results = context["gene_results"]
        accessions = set()
        for gene_id, gene_data in gene_results.items():
            if gene_id == "gene_entrez_ids" or not gene_data:
                continue
            if "UniProt_name" in gene_data:
                accessions.add(gene_data["UniProt_name"])
        return sorted(accessions)

    def fetch_one(self, protein_accession):
        """Fetch raw UniProt JSON for a protein accession.

        Parameters
        ----------
        protein_accession : str
            UniProt protein accession identifier

        Returns
        -------
        dict
            Raw UniProt API response
        """
        response = requests.get(
            f"https://rest.uniprot.org/uniprotkb/{protein_accession}",
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            print(
                f"[{self.name}] Assigning results for "
                f"protein accession {protein_accession}"
            )
            return response.json()
        else:
            print(
                f"[{self.name}] Could not assign results for "
                f"protein accession {protein_accession}"
            )
            return {}

    def before_dump(self, results, ids):
        """Store the protein accession list in the results."""
        results["protein_accessions"] = ids


HUBMAP_DIRPATH = EXTERNAL_DIRPATH / "hubmap"
HUBMAP_LATEST_URLS = [
    "https://lod.humanatlas.io/asct-b/allen-brain/latest/",
    "https://lod.humanatlas.io/asct-b/eye/latest/",
    "https://lod.humanatlas.io/asct-b/kidney/latest/",
    "https://lod.humanatlas.io/asct-b/lung/latest/",
    "https://lod.humanatlas.io/asct-b/pancreas/latest/",
]


class HuBMAPFetcher(DataFetcher):
    """Downloads HuBMAP ASCT+B data table JSON files, archiving
    earlier versions."""

    name = "hubmap"
    output_path = HUBMAP_DIRPATH

    def get_ids(self, context):
        """Not used -- HuBMAP overrides run()."""
        return []

    def fetch_one(self, id_value):
        """Not used -- HuBMAP overrides run()."""
        return {}

    def run(self, context, force=False):
        """Download latest HuBMAP data table JSON files, archiving
        any earlier versions.

        Parameters
        ----------
        context : dict
            Shared pipeline state (unused)
        force : bool
            Unused (HuBMAP checks file existence directly)

        Returns
        -------
        dict
            Empty dict (files are saved directly to disk)
        """
        json_urls = self._get_hubmap_json_urls()
        for org, ver, url in json_urls:
            hubmap_filepath = HUBMAP_DIRPATH / f"{org}-v{ver}.json"
            if hubmap_filepath.exists():
                print(f"HuBMAP data table {hubmap_filepath} already exists")
                continue

            archive_dirpath = HUBMAP_DIRPATH / ".archive"
            os.makedirs(archive_dirpath, exist_ok=True)
            for pathname in glob(str(HUBMAP_DIRPATH / f"{org}-v*.json")):
                try:
                    shutil.move(Path(pathname), archive_dirpath)
                    print(f"Archived HuBMAP data table {pathname}")
                except Exception:
                    os.remove(pathname)
                    print(f"Removed HuBMAP data table {pathname}")

            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                with open(hubmap_filepath, "w") as fp:
                    fp.write(response.text)
                print(f"Downloaded HuBMAP data table {hubmap_filepath}")
            else:
                print(f"Could not download HuBMAP data table {hubmap_filepath}")

        return {}

    @staticmethod
    def _get_hubmap_json_urls():
        """Get the URL to specified HuBMAP data table JSON files.

        Returns
        -------
        list
            List of (organ, version, url) tuples
        """
        json_urls = []
        p_org = re.compile(r"asct-b\/(.*)\/latest")
        p_url = re.compile(r"https:\/\/.*\/v(\d\.\d)\/graph.json")
        for latest_url in HUBMAP_LATEST_URLS:
            m_org = p_org.search(latest_url)
            if m_org is not None:
                org = m_org.group(1)
            else:
                raise Exception("No organ in HuBMAP URL")

            response = requests.get(latest_url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                m_url = p_url.search(response.text)
                if m_url is not None:
                    json_url = m_url.group(0)
                    json_ver = float(m_url.group(1))
                    json_urls.append((org, json_ver, json_url))
                else:
                    raise Exception("Could not find HuBMAP JSON URL or version")
            else:
                raise Exception("Could not get HuBMAP latest URL")

        return json_urls


FETCHER_REGISTRY = [
    CellxGeneFetcher(),
    OpenTargetsFetcher(),
    GeneFetcher(),
    UniProtFetcher(),
    HuBMAPFetcher(),
]


CELLXGENE_PATH = CellxGeneFetcher.output_path
OPENTARGETS_PATH = OpenTargetsFetcher.output_path
GENE_PATH = GeneFetcher.output_path
UNIPROT_PATH = UniProtFetcher.output_path


def main():
    """Fetch external API results for all registered sources.

    Builds a shared context from NSForest results and dataset metadata,
    then runs each fetcher in registry order. Results from earlier
    fetchers are available to later ones via the context dict.
    """
    parser = argparse.ArgumentParser(description="Fetch External API Results")
    for fetcher in FETCHER_REGISTRY:
        parser.add_argument(
            f"--force-{fetcher.name}",
            action="store_true",
            help=f"force fetching of {fetcher.name} results",
        )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="force fetching of all results",
    )
    parser.add_argument(
        "--results-sources",
        type=Path,
        default=None,
        help="path to results-sources JSON file (default: use LoaderUtilities default)",
    )
    args = parser.parse_args()

    # Build shared context
    results_sources = (
        get_results_sources(args.results_sources)
        if args.results_sources
        else get_results_sources()
    )
    harvester_data = get_cellxgene_harvester_data(results_sources)
    file_paths = get_dataset_file_paths(results_sources)
    dataset_version_id_lists = get_dataset_version_id_lists(file_paths)
    gene_data = get_unique_gene_names_and_ids(file_paths["nsforest_paths"])

    context = {
        "gene_data": gene_data,
        "file_paths": file_paths,
        "dataset_version_id_lists": dataset_version_id_lists,
    }

    for fetcher in FETCHER_REGISTRY:
        force_flag = f"force_{fetcher.name}"
        force = getattr(args, force_flag, False) or args.force_all
        result = fetcher.run(context, force=force)
        context[f"{fetcher.name}_results"] = result


if __name__ == "__main__":
    main()
