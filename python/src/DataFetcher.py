import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from datetime import datetime, timezone
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
    get_unique_gene_names_and_ids,
    set_current_run,
)


REQUEST_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER = 5  # seconds
MAX_FETCH_ATTEMPTS = 3  # per-ID attempts, across runs, before giving up


class DataFetcher:
    """Base class for all external API data fetchers. Subclasses implement
    get_ids() and fetch_one(). The batch loop, checkpointing, and CLI
    integration are handled here."""

    name: str
    batch_size: int = 25
    max_per_second: int = 5
    request_timeout: int = REQUEST_TIMEOUT

    max_fetch_attempts: int = MAX_FETCH_ATTEMPTS

    _output_path_override = None

    # Outcome of the most recent run(), read by main() to derive the
    # source's status. n_retryable counts failed IDs still under the
    # attempt cap, which the next run will re-fetch.
    n_failed = 0
    n_retryable = 0
    n_exhausted = 0

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

    @property
    def failures_path(self):
        """Path to the sidecar mapping each failed ID to its attempt count.

        The suffix is deliberately not ``.json`` so that cache cleaning and
        validation, which glob ``*.json``, do not mistake it for a result.
        """
        return self.output_path.with_suffix(".failures")

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
            failures = {}
            if self.checkpoint_path.exists():
                self.checkpoint_path.unlink()
            if self.failures_path.exists():
                self.failures_path.unlink()
        else:
            results = self._load()
            failures = self._load_failures()

        ids = self.get_ids(context)
        # An ID that failed holds its on_fetch_error fallback in results, so
        # it is pending again until it succeeds or reaches the attempt cap.
        pending_ids = [
            id_val
            for id_val in ids
            if id_val not in results
            or failures.get(id_val, 0) in range(1, self.max_fetch_attempts)
        ]
        total = len(ids)

        self.n_failed = 0
        if not pending_ids:
            self._summarize_failures(failures, ids)
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
                        failures.pop(id_value, None)
                    except (
                        requests.RequestException,
                        ValueError,
                        KeyError,
                        RuntimeError,
                    ) as exc:
                        print(f"[{self.name}] Error fetching {id_value}: {exc}")
                        result = self.on_fetch_error(id_value)
                        failures[id_value] = failures.get(id_value, 0) + 1
                        self.n_failed += 1
                    except Exception as exc:
                        warnings.warn(
                            f"[{self.name}] Unexpected error fetching"
                            f" {id_value}: {exc!r}"
                        )
                        result = self.on_fetch_error(id_value)
                        failures[id_value] = failures.get(id_value, 0) + 1
                        self.n_failed += 1

                    results[id_value] = result
                    batch[id_value] = result

                print(
                    f"[{self.name}] Completed"
                    f" {n_completed}/{len(pending_ids)}"
                    f" - {total - len(pending_ids) + n_completed}"
                    f"/{total} total"
                )

                # Failures are saved first: if a crash lands between the two
                # writes, the worst case is a batch re-fetched next run, not a
                # fallback silently accepted as fetched (CodeRabbit #103).
                # This does not cover retrying a *completed* prior run (results
                # loaded from output_path, not the checkpoint) that is then
                # itself interrupted in the same window; closing that would
                # mean storing the attempt count on each result rather than
                # in a separate sidecar.
                self._save_failures(failures)
                self._save_checkpoint(batch)

        self._summarize_failures(failures, ids)
        self._save_final(results, ids)
        return results

    def _summarize_failures(self, failures, ids):
        """Set the failure counters for this run and report IDs left failed.

        Parameters
        ----------
        failures : dict
            Mapping of ID to failed-attempt count
        ids : list
            The current list of IDs; failures for IDs no longer wanted are
            dropped
        """
        wanted = set(ids)
        for id_value in [k for k in failures if k not in wanted]:
            del failures[id_value]
        self.n_retryable = sum(
            1 for n in failures.values() if n < self.max_fetch_attempts
        )
        self.n_exhausted = len(failures) - self.n_retryable
        self._save_failures(failures)
        if failures:
            print(
                f"[{self.name}] {len(failures)} failed IDs:"
                f" {self.n_retryable} will be retried,"
                f" {self.n_exhausted} gave up after"
                f" {self.max_fetch_attempts} attempts"
            )

    def _load_failures(self):
        """Load the failed-attempt counts written by an earlier run.

        An existing sidecar that cannot be parsed raises rather than being
        treated as "no failures": silently discarding it would let cached
        on_fetch_error fallbacks be treated as fetched, and the run would
        then overwrite the sidecar with an empty one (CodeRabbit #103),
        erasing the evidence needed to retry them.

        Returns
        -------
        dict
            Mapping of ID to failed-attempt count, or empty dict if no
            sidecar exists

        Raises
        ------
        RuntimeError
            If the sidecar exists but cannot be read or parsed
        """
        if not self.failures_path.exists():
            return {}
        try:
            with open(self.failures_path, "r") as fp:
                return {k: int(v) for k, v in json.load(fp).items()}
        except (OSError, ValueError, AttributeError) as exc:
            raise RuntimeError(
                f"[{self.name}] Could not read {self.failures_path}: {exc}."
                " Fix or remove the file, then re-run; the fetcher will not"
                " guess which cached results are actually failures."
            ) from exc

    def _save_failures(self, failures):
        """Write the failed-attempt counts, or remove the file if none.

        Writes via a temp file and ``os.replace`` so a crash mid-write never
        leaves a partially-written (unparseable) sidecar behind.

        Parameters
        ----------
        failures : dict
            Mapping of ID to failed-attempt count
        """
        if failures:
            self.failures_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.failures_path.with_suffix(
                self.failures_path.suffix + ".tmp"
            )
            with open(tmp_path, "w") as fp:
                json.dump(failures, fp)
            os.replace(tmp_path, self.failures_path)
        elif self.failures_path.exists():
            self.failures_path.unlink()

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

        The current collection is fetched, and so is the collection version
        the dataset version belongs to.  The two differ once a collection is
        revised: the current one describes the latest publication, the version
        the one the dataset version was published with (e.g. a bioRxiv
        preprint later replaced by its journal paper).

        Parameters
        ----------
        dataset_version_id : str
            CELLxGENE dataset version identifier

        Returns
        -------
        dict
            Raw response with keys 'dataset_json', 'collection_json' and
            'collection_version_json'; the latter is None when the dataset
            names no collection version or the API does not serve it
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

        collection_version_json = None
        collection_version_id = dataset_json.get("collection_version_id")
        if collection_version_id:
            version_url = f"{self.BASE_URL}/collection_versions/{collection_version_id}"
            resp = requests.get(version_url, timeout=REQUEST_TIMEOUT)
            # A version the API does not serve is not an error: the transformer
            # falls back to the current collection.  Anything else is retried.
            if resp.status_code not in (403, 404):
                resp.raise_for_status()
                collection_version_json = resp.json()

        return {
            "dataset_json": dataset_json,
            "collection_json": collection_json,
            "collection_version_json": collection_version_json,
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
        if not context.get("gene_data"):
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
        if not context.get("gene_data"):
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
        failures = []
        p_org = re.compile(r"asct-b\/(.*)\/latest")
        p_url = re.compile(r"https:\/\/.*\/v(\d\.\d)\/graph.json")
        for latest_url in urls:
            if not latest_url.startswith(("http://", "https://")):
                failures.append(f"Invalid URL (missing scheme): {latest_url!r}")
                continue

            m_org = p_org.search(latest_url)
            if m_org is None:
                failures.append(f"No organ found in HuBMAP URL: {latest_url!r}")
                continue
            org = m_org.group(1)

            try:
                response = requests.get(latest_url, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as exc:
                failures.append(f"Request failed for {latest_url!r}: {exc}")
                continue

            if response.status_code != 200:
                failures.append(f"HTTP {response.status_code} for {latest_url!r}")
                continue

            m_url = p_url.search(response.text)
            if m_url is None:
                failures.append(
                    f"Could not find JSON URL in response for {latest_url!r}"
                )
                continue

            json_url = m_url.group(0)
            json_ver = m_url.group(1)
            json_urls.append((org, json_ver, json_url))

        if failures:
            print(
                "WARNING: the following HuBMAP URLs could not be resolved:\n"
                + "\n".join(f"  - {f}" for f in failures)
            )
        if not json_urls and failures:
            raise Exception(f"All {len(failures)} HuBMAP URL(s) failed to resolve.")
        if not json_urls:
            print("WARNING: No HuBMAP URLs configured — skipping HuBMAP fetch.")

        return json_urls


def _load_fetch_status(path: Path) -> dict:
    """Load per-source fetch status from fetch-status.json, or return empty dict."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Could not read fetch status from %s: %s", path, e
            )
    return {}


def _save_fetch_status(path: Path, status: dict) -> None:
    """Persist per-source fetch status to fetch-status.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2))


def _is_freshness_skip(source_status, max_age_hours, now_utc):
    """Decide whether a source's freshness check should skip a live fetch.

    A source whose last outcome is "partial" still has failed IDs under the
    retry cap; its last_success_at is deliberately left stale (see
    DataFetcher.run) precisely so those retries are not skipped here
    (CodeRabbit #103). A forced run that leaves a source partial can
    therefore still have an old, otherwise-fresh last_success_at, so the
    outcome is checked before the age.

    Parameters
    ----------
    source_status : dict
        This source's entry in fetch-status.json (``last_outcome``,
        ``last_success_at``)
    max_age_hours : float
        Skip threshold in hours
    now_utc : datetime
        Current time, timezone-aware UTC

    Returns
    -------
    tuple[bool, float | None]
        Whether to skip, and the age in hours (None if not applicable)
    """
    if source_status.get("last_outcome") == "partial":
        return False, None
    last_success = source_status.get("last_success_at")
    if not last_success:
        return False, None
    age_hours = (now_utc - datetime.fromisoformat(last_success)).total_seconds() / 3600
    return age_hours < max_age_hours, age_hours


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
        "--run-name",
        default=None,
        help="run name (selects data/results-<name>/; defaults to $CKN_RUN or 'full')",
    )
    parser.add_argument(
        "--max-source-age-hours",
        type=float,
        default=0.0,
        help=(
            "Skip sources whose last successful fetch is younger than this many hours. "
            "0 (default) disables freshness checking and always re-fetches every source."
        ),
    )
    args = parser.parse_args()

    if args.max_source_age_hours < 0:
        parser.error("--max-source-age-hours must be non-negative")

    set_current_run(args.run_name)

    # Per-source fetch status — written alongside the cache files so it travels to S3
    status_path = get_current_run().external_dir / "fetch-status.json"
    status = _load_fetch_status(status_path)
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # Build shared context from the flat release zip directory
    results_dir = get_current_run().results_dir
    try:
        file_paths = get_dataset_file_paths(results_dir)
    except Exception as exc:
        print(f"WARNING: Could not read results directory ({results_dir.name}/): {exc}")
        file_paths = {
            "nsforest_paths": [],
            "mapping_paths": [],
            "scores_paths": [],
            "summary_paths": [],
            "binary_scores_paths": [],
        }

    if file_paths["nsforest_paths"]:
        try:
            dataset_version_id_lists = get_dataset_version_id_lists(file_paths)
        except Exception as exc:
            print(
                f"WARNING: Failed to build dataset version ID lists: {exc}\n"
                "Fetchers that depend on dataset version IDs will receive an empty list."
            )
            dataset_version_id_lists = []
    else:
        print(
            f"WARNING: No results_ensg_*.csv files found in {results_dir.name}/ — "
            "fetchers that depend on NSForest results will produce no output."
        )
        dataset_version_id_lists = []

    if file_paths["nsforest_paths"]:
        try:
            gene_data = get_unique_gene_names_and_ids(file_paths["nsforest_paths"])
        except Exception as exc:
            print(
                f"WARNING: Failed to build gene data context (BioMart or other error): {exc}\n"
                "Fetchers that require gene data (gene, uniprot, opentargets) will be skipped."
            )
            gene_data = None
    else:
        gene_data = None

    context = {
        "gene_data": gene_data,
        "file_paths": file_paths,
        "dataset_version_id_lists": dataset_version_id_lists,
    }

    failed_fetchers = []
    for fetcher in FETCHER_REGISTRY:
        force_flag = f"force_{fetcher.name}"
        force = getattr(args, force_flag, False) or args.force_all

        # Freshness check: skip sources that succeeded recently (unless forced
        # or left with retryable failures — see _is_freshness_skip)
        if not force and args.max_source_age_hours > 0:
            skip, age_hours = _is_freshness_skip(
                status.get(fetcher.name, {}), args.max_source_age_hours, now_utc
            )
            if skip:
                print(
                    f"[{fetcher.name}] Skipping — last success"
                    f" {age_hours:.1f}h ago (max age: {args.max_source_age_hours}h)"
                )
                status.setdefault(fetcher.name, {})["last_outcome"] = "skipped"
                _save_fetch_status(status_path, status)
                # Load cached results so downstream fetchers (e.g. uniprot → gene) still work
                if fetcher.output_path.is_dir():
                    context[f"{fetcher.name}_results"] = {}
                else:
                    context[f"{fetcher.name}_results"] = fetcher._load()
                continue

        # Check cache before attempting a live fetch when context is incomplete.
        # Fetchers like opentargets and gene require gene_data; if BioMart failed,
        # gene_data is None and get_ids() will raise. Use cached output if available
        # rather than failing the whole run.
        try:
            fetcher.get_ids(context)
        except (ValueError, KeyError) as exc:
            cached = fetcher._load()
            if cached:
                print(f"[{fetcher.name}] {exc}; using cached results")
                context[f"{fetcher.name}_results"] = cached
                status.setdefault(fetcher.name, {})["last_outcome"] = "cached"
                _save_fetch_status(status_path, status)
                continue
            print(f"ERROR: Fetcher '{fetcher.name}' requires missing context: {exc}")
            context[f"{fetcher.name}_results"] = {}
            status.setdefault(fetcher.name, {})["last_outcome"] = "failed"
            _save_fetch_status(status_path, status)
            failed_fetchers.append(fetcher.name)
            continue

        # Record that we are attempting this source
        status.setdefault(fetcher.name, {})["last_attempt_at"] = now_iso
        _save_fetch_status(status_path, status)

        try:
            result = fetcher.run(context, force=force)
            context[f"{fetcher.name}_results"] = result
            if fetcher.n_retryable:
                # Failed IDs remain under the attempt cap. Leave
                # last_success_at alone so the freshness check does not skip
                # this source before they are retried.
                print(
                    f"[{fetcher.name}] Partial: {fetcher.n_retryable} failed"
                    " IDs will be retried on the next run"
                )
                status[fetcher.name]["last_outcome"] = "partial"
            else:
                status[fetcher.name]["last_success_at"] = now_iso
                status[fetcher.name]["last_outcome"] = "ok"
            _save_fetch_status(status_path, status)
        except Exception as exc:
            print(f"ERROR: Fetcher '{fetcher.name}' failed and will be skipped: {exc}")
            context[f"{fetcher.name}_results"] = {}
            status[fetcher.name]["last_outcome"] = "failed"
            _save_fetch_status(status_path, status)
            failed_fetchers.append(fetcher.name)

    if failed_fetchers:
        raise RuntimeError(
            f"The following fetcher(s) failed: {', '.join(failed_fetchers)}. "
            "Partial results have been saved for all other sources."
        )


if __name__ == "__main__":
    main()
