import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from glob import glob
from pathlib import Path
import re
import shutil
from time import sleep
import warnings

import requests

import base64
import gzip

from E_Utilities import extract_uniprot_name, fetch_xml_for_gene_id
from OpenTargetsGGetQueries import gget_queries
from LoaderUtilities import (
    OPENTARGETS_RESOURCES,
    get_current_run,
    get_dataset_file_paths,
    get_dataset_version_id_lists,
    get_results_sources,
    get_unique_gene_names_and_ids,
    set_current_run,
)


REQUEST_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER = 5  # seconds


class DataFetcher:
    """Base class for all external API data fetchers. Subclasses implement
    get_ids() and fetch_one(). The batch loop, checkpointing, and CLI
    integration are handled here."""

    name: str
    batch_size: int = 25
    max_per_second: int = 5
    request_timeout: int = REQUEST_TIMEOUT

    _output_path_override = None

    @property
    def output_path(self):
        """Path where final results are written. Defaults to
        ``<run external_dir>/<name>.json`` but may be overridden by
        assignment (e.g. from tests)."""
        if self._output_path_override is not None:
            return self._output_path_override
        return get_current_run().external_dir / f"{self.name}.json"

    @output_path.setter
    def output_path(self, value):
        self._output_path_override = value

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

    def _fetch_with_retry(self, id_value):
        """Call fetch_one, retrying on HTTP 429 (Too Many Requests).

        Respects the Retry-After header if present, otherwise waits
        DEFAULT_RETRY_AFTER seconds.  Gives up after MAX_RETRIES
        consecutive 429s.

        Parameters
        ----------
        id_value : str
            The identifier to fetch

        Returns
        -------
        dict
            Raw API response data
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self.fetch_one(id_value)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    wait = int(
                        exc.response.headers.get("Retry-After", DEFAULT_RETRY_AFTER)
                    )
                    print(
                        f"[{self.name}] 429 for {id_value},"
                        f" retry {attempt}/{MAX_RETRIES}"
                        f" after {wait}s"
                    )
                    sleep(wait)
                else:
                    raise
        return self.fetch_one(id_value)

    @property
    def checkpoint_path(self):
        """Path to the JSONL checkpoint file."""
        return self.output_path.with_suffix(".jsonl")

    def run(self, context, force=False):
        """Execute the fetch loop with batch-save checkpointing.

        Fetches are submitted to a thread pool at a rate of
        ``max_per_second`` to stay under API rate limits. New results
        are appended to a JSONL checkpoint file every ``batch_size``
        completions. Once all fetches are done, the final JSON output
        is written and the checkpoint is removed.

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
        if force:
            results = {}
            if self.checkpoint_path.exists():
                self.checkpoint_path.unlink()
        else:
            results = self._load()

        ids = self.get_ids(context)
        pending_ids = [id_val for id_val in ids if id_val not in results]
        total = len(ids)

        if not pending_ids:
            print(f"[{self.name}] All {total} IDs already fetched")
            return results

        print(
            f"[{self.name}] {total - len(pending_ids)} already fetched,"
            f" {len(pending_ids)} remaining"
        )

        interval = 1.0 / self.max_per_second
        n_completed = 0

        with ThreadPoolExecutor(max_workers=self.max_per_second) as pool:
            for batch_start in range(0, len(pending_ids), self.batch_size):
                batch_ids = pending_ids[batch_start : batch_start + self.batch_size]
                futures = {}
                for id_value in batch_ids:
                    futures[pool.submit(self._fetch_with_retry, id_value)] = id_value
                    sleep(interval)

                batch = {}
                for future in as_completed(futures):
                    id_value = futures[future]
                    n_completed += 1

                    try:
                        result = future.result()
                    except (
                        requests.RequestException,
                        ValueError,
                        KeyError,
                        RuntimeError,
                    ) as exc:
                        print(f"[{self.name}] Error fetching {id_value}: {exc}")
                        result = self.on_fetch_error(id_value)
                    except Exception as exc:
                        warnings.warn(
                            f"[{self.name}] Unexpected error fetching"
                            f" {id_value}: {exc!r}"
                        )
                        result = self.on_fetch_error(id_value)

                    results[id_value] = result
                    batch[id_value] = result

                print(
                    f"[{self.name}] Completed"
                    f" {n_completed}/{len(pending_ids)}"
                    f" - {total - len(pending_ids) + n_completed}"
                    f"/{total} total"
                )

                self._save_checkpoint(batch)

        self._save_final(results, ids)
        return results

    def _load(self):
        """Load results from the JSONL checkpoint (if an incomplete run
        exists) or from the final JSON output.

        Returns
        -------
        dict
            Previously fetched results, or empty dict
        """
        if self.checkpoint_path.exists():
            print(f"[{self.name}] Resuming from checkpoint {self.checkpoint_path}")
            results = {}
            with open(self.checkpoint_path, "r") as fp:
                for line in fp:
                    line = line.strip()
                    if line:
                        record = json.loads(line)
                        results[record["id"]] = record["data"]
            if self.output_path.exists():
                print(
                    f"[{self.name}] Merging with prior results from {self.output_path}"
                )
                with open(self.output_path, "r") as fp:
                    prior = json.load(fp)
                for key, value in prior.items():
                    if key not in results:
                        results[key] = value
            return results
        elif self.output_path.exists():
            print(f"[{self.name}] Loading results from {self.output_path}")
            with open(self.output_path, "r") as fp:
                return json.load(fp)
        return {}

    def _save_checkpoint(self, batch):
        """Append a batch of new results to the JSONL checkpoint file.

        Parameters
        ----------
        batch : dict
            Mapping of ID to result data for newly completed fetches
        """
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{self.name}] Appending {len(batch)} records to {self.checkpoint_path}")
        with open(self.checkpoint_path, "a") as fp:
            for id_value, data in batch.items():
                fp.write(json.dumps({"id": id_value, "data": data}) + "\n")

    def _save_final(self, results, ids):
        """Write the final JSON output and remove the checkpoint.

        Parameters
        ----------
        results : dict
            The full results dict
        ids : list
            The full list of IDs (passed to before_dump)
        """
        self.before_dump(results, ids)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{self.name}] Writing final results to {self.output_path}")
        with open(self.output_path, "w") as fp:
            json.dump(results, fp, indent=4)
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
            print(f"[{self.name}] Removed checkpoint {self.checkpoint_path}")


class CellxGeneFetcher(DataFetcher):
    """Fetches dataset and collection metadata from the CELLxGENE
    curation API."""

    name = "cellxgene"

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
        response.raise_for_status()

        dataset_json = response.json()

        collection_json = None
        collection_id = dataset_json.get("collection_id")
        if collection_id:
            collection_url = f"{self.BASE_URL}/collections/{collection_id}"
            resp = requests.get(collection_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
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
    max_per_second = 2
    request_timeout = 120

    def get_ids(self, context):
        """Return gene Ensembl IDs."""
        if "gene_data" not in context:
            raise ValueError("OpenTargetsFetcher requires 'gene_data' in context")
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
        variables = {**query["variables"], "ensemblId": gene_ensembl_id}
        response = requests.post(
            OPENTARGETS_BASE_URL,
            json={
                "query": query["query_string"],
                "variables": variables,
            },
            timeout=self.request_timeout,
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
    # max_per_second = 10  # NCBI allows 10 req/s with API key, but seems to return 429 at times

    def get_ids(self, context):
        """Return gene Entrez IDs."""
        if "gene_data" not in context:
            raise ValueError("GeneFetcher requires 'gene_data' in context")
        return context["gene_data"]["gene_entrez_ids"]

    def fetch_one(self, gene_entrez_id):
        """Fetch raw XML for a gene Entrez ID, compress, and base64
        encode for JSON storage. Also extracts the UniProt accession
        so that UniProtFetcher can run without a prior parse step.

        Parameters
        ----------
        gene_entrez_id : str
            NCBI Gene Entrez identifier

        Returns
        -------
        dict
            Dict with 'xml_gz_b64' (compressed XML) and
            'UniProt_name' (protein accession, or None)
        """
        print(f"[{self.name}] Fetching gene XML for gene Entrez id {gene_entrez_id}")
        xml_data = fetch_xml_for_gene_id(gene_entrez_id)
        if xml_data is None:
            raise RuntimeError(
                f"Failed to fetch gene XML for Entrez id {gene_entrez_id}"
            )
        compressed = gzip.compress(xml_data.encode("utf-8"))
        return {
            "xml_gz_b64": base64.b64encode(compressed).decode("ascii"),
            "UniProt_name": extract_uniprot_name(xml_data),
        }

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
            if gene_data.get("UniProt_name"):
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
        response.raise_for_status()
        print(
            f"[{self.name}] Assigning results for protein accession {protein_accession}"
        )
        return response.json()

    def before_dump(self, results, ids):
        """Store the protein accession list in the results."""
        results["protein_accessions"] = ids


class HuBMAPFetcher(DataFetcher):
    """Downloads HuBMAP ASCT+B data table JSON files, archiving
    earlier versions."""

    name = "hubmap"

    @property
    def output_path(self):
        """Directory where HuBMAP JSON files are written."""
        if self._output_path_override is not None:
            return self._output_path_override
        return get_current_run().external_dir / "hubmap"

    @output_path.setter
    def output_path(self, value):
        self._output_path_override = value

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
        hubmap_dir = self.output_path
        hubmap_dir.mkdir(parents=True, exist_ok=True)
        json_urls = self._get_hubmap_json_urls()
        for org, ver, url in json_urls:
            hubmap_filepath = hubmap_dir / f"{org}-v{ver}.json"
            if hubmap_filepath.exists():
                print(f"HuBMAP data table {hubmap_filepath} already exists")
                continue

            archive_dirpath = hubmap_dir / ".archive"
            os.makedirs(archive_dirpath, exist_ok=True)
            for pathname in glob(str(hubmap_dir / f"{org}-v*.json")):
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
    def _get_hubmap_json_urls(urls=None):
        """Get the URL to specified HuBMAP data table JSON files.

        Parameters
        ----------
        urls : list of str, optional
            HuBMAP "latest" URLs to resolve. Defaults to the list in
            the current run config.

        Returns
        -------
        list
            List of (organ, version, url) tuples
        """
        if urls is None:
            urls = get_current_run().hubmap_urls
        json_urls = []
        p_org = re.compile(r"asct-b\/(.*)\/latest")
        p_url = re.compile(r"https:\/\/.*\/v(\d\.\d)\/graph.json")
        for latest_url in urls:
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
        "--run",
        default=None,
        help="run name (selects data/run-<name>.json; "
        "defaults to $CKN_RUN or 'full')",
    )
    args = parser.parse_args()

    set_current_run(args.run)

    # Build shared context
    results_sources = get_results_sources()
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
