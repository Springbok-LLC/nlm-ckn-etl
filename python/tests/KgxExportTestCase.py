"""Tests for the KGX TSV export in flows/pipeline.py.

Covers:
- export_kgx: one KGX transform per database, against the live (non-default)
  port, with all_collections and the upload-neo4j.sh basenames; NLM-CKN
  provenance, and each edge's Source, as infores CURIEs, in
  primary_knowledge_source, knowledge_source and supporting_data_source
- sync_kgx_to_s3 / sync_golden_dump_from_s3: exact S3 keys, local-mode no-ops
- nlm_ckn_etl flag matrix: --force-kgx alone, --run-archive alone, both,
  and no flags at all
"""

import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make both python/src and python/src/flows importable.
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

from pipeline import (  # noqa: E402
    export_kgx,
    nlm_ckn_etl,
    sync_golden_dump_from_s3,
    sync_kgx_to_s3,
)


def _noop_logger():
    """Return a silent MagicMock that satisfies logger calls inside tasks."""
    m = MagicMock()
    m.info = lambda *a, **kw: None
    m.warning = lambda *a, **kw: None
    m.error = lambda *a, **kw: None
    return m


class ExportKgxTestCase(unittest.TestCase):
    """Tests for the export_kgx task."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo_root = Path(tmp.name)
        self.kgx_dir = self.repo_root / "data" / "kgx-1.2.3"

    def _call(self, port=54321, edges=None, host="127.0.0.1", scheme="http"):
        """Run export_kgx against a mocked Transformer; return one mock per database.

        ``edges`` is the ``(u, v, key, data)`` list each database's in-memory
        store yields, standing in for the graph KGX loads from ArangoDB.
        """
        transformers = []

        def make_transformer(*a, **kw):
            t = MagicMock()
            t.store.graph.edges.return_value = [
                (u, v, k, dict(d)) for u, v, k, d in (edges or [])
            ]
            t.store.edge_properties = {"id", "subject", "predicate", "object"}
            transformers.append(t)
            return t

        with patch("pipeline.get_run_logger", return_value=_noop_logger()), \
             patch("pipeline.REPO_ROOT", self.repo_root), \
             patch("_common.ARANGO_DB_HOST", host), \
             patch("_common.ARANGO_DB_PORT", port), \
             patch("_common.ARANGO_DB_SCHEME", scheme), \
             patch("kgx.transformer.Transformer", side_effect=make_transformer):
            export_kgx.fn(self.kgx_dir, "secret")
        return transformers

    def _source_configs(self, transformers):
        return [t.transform.call_args.args[0] for t in transformers]

    def test_reads_each_database_on_live_port(self):
        """One non-streaming transform per database, on the dynamic port, not 8529."""
        transformers = self._call(port=54321)
        configs = self._source_configs(transformers)
        self.assertEqual(
            [c["database"] for c in configs], ["Cell-KN-Ontologies", "Cell-KN-Phenotypes"]
        )
        for config in configs:
            self.assertEqual(config["uri"], "http://127.0.0.1:54321")
            self.assertEqual(config["format"], "arangodb")
            self.assertEqual((config["username"], config["password"]), ("root", "secret"))
            self.assertIs(config["all_collections"], True)

    def test_reads_remote_host_over_https(self):
        """A remote host is read with the scheme _common resolved for it."""
        transformers = self._call(host="10.0.1.5", scheme="https")
        for config in self._source_configs(transformers):
            self.assertEqual(config["uri"], "https://10.0.1.5:54321")

    def test_writes_upload_neo4j_basenames(self):
        """Saves TSV to <kgx_dir>/<db>, which the sink suffixes with _nodes/_edges."""
        transformers = self._call()
        self.assertEqual(
            [t.save.call_args.args[0] for t in transformers],
            [
                {"filename": str(self.kgx_dir / db), "format": "tsv", "compression": None}
                for db in ("Cell-KN-Ontologies", "Cell-KN-Phenotypes")
            ],
        )

    def test_provenance_is_nlm_ckn_not_arango_uri(self):
        """Sets node provided_by and edge aggregator, so KGX never falls back to the URI."""
        for config in self._source_configs(self._call()):
            self.assertEqual(config["provided_by"], "infores:nlm-ckn")
            self.assertEqual(config["aggregator_knowledge_source"], "infores:nlm-ckn")
            # No static knowledge_source: it is set per edge from Source.
            self.assertNotIn("knowledge_source", config)

    def test_knowledge_sources_translated_from_source(self):
        """Each edge's Source, as infores CURIEs, lands in the Biolink provenance slots.

        The first CURIE is the scalar primary_knowledge_source (and
        knowledge_source); a promoted list maps element-wise, dedupes, and puts
        the rest in supporting_data_source.  Names with no CURIE are dropped,
        and an edge left with none (or with no Source) gets knowledge_source
        infores:nlm-ckn and no primary.  knowledge_source is on every edge:
        KGX loaders fill a missing one with the input filename.
        """
        edges = [
            ("CS:1", "GS:A", "e1", {"Source": "NS-Forest"}),
            ("GS:A", "PR:1", "e2", {"Source": "Open Targets and Gene"}),
            ("CL:1", "CL:2", "e3", {"Source": ["CL", "CELLxGENE", "UBERON", "cl"]}),
            ("CS:1", "CL:1", "e4", {"Source": "CELLxGENE"}),
            ("CL:1", "CL:3", "e5", {}),
        ]
        slots = ("primary_knowledge_source", "knowledge_source", "supporting_data_source")
        expected = [
            {slots[0]: "infores:nlm-ckn", slots[1]: "infores:nlm-ckn"},
            {slots[0]: "infores:open-targets", slots[1]: "infores:open-targets"},
            {
                slots[0]: "infores:cl",
                slots[1]: "infores:cl",
                slots[2]: ["infores:uberon"],
            },
            {slots[1]: "infores:nlm-ckn"},
            {slots[1]: "infores:nlm-ckn"},
        ]
        transformers = self._call(edges=edges)
        for t in transformers:
            written = [d for *_, d in t.store.graph.edges.return_value]
            self.assertEqual(
                [{s: d[s] for s in slots if s in d} for d in written], expected
            )
            for slot in slots:
                self.assertIn(slot, t.store.edge_properties)
            # The Source column itself is left as-is.
            self.assertEqual(written[0]["Source"], "NS-Forest")
            # The copy must happen before the TSV is written.
            self.assertLess(
                [c[0] for c in t.method_calls].index("store.graph.edges"),
                [c[0] for c in t.method_calls].index("save"),
            )

    def test_schema_database_not_exported(self):
        """Cell-KN-Schema is a metamodel and is deliberately excluded."""
        exported = [c["database"] for c in self._source_configs(self._call())]
        self.assertNotIn("Cell-KN-Schema", exported)
        self.assertNotIn("_system", exported)

    def test_stale_export_cleared(self):
        """A previous export in kgx_dir is removed so two runs never blend."""
        self.kgx_dir.mkdir(parents=True)
        stale = self.kgx_dir / "stale_nodes.tsv"
        stale.write_text("old\n")
        self._call()
        self.assertTrue(self.kgx_dir.is_dir())
        self.assertFalse(stale.exists())


class SyncKgxToS3TestCase(unittest.TestCase):
    """Tests for the sync_kgx_to_s3 task."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.kgx_dir = Path(tmp.name) / "kgx-1.2.3"
        self.kgx_dir.mkdir()
        (self.kgx_dir / "Cell-KN-Phenotypes_nodes.tsv").write_text("id\n")

    def _call(self, bucket):
        with patch("pipeline.get_run_logger", return_value=_noop_logger()), \
             patch("pipeline.S3_BUCKET", bucket), \
             patch("_common.S3_BUCKET", bucket), \
             patch("_common.boto3") as mock_boto3:
            sync_kgx_to_s3.fn(self.kgx_dir, run_name="1.2.3")
        return mock_boto3.client.return_value

    def test_uploads_to_stage_07_key(self):
        """Uploads exactly one object, at runs/{run}/07-kgx.tar.gz."""
        mock_s3 = self._call("my-bucket")
        mock_s3.upload_file.assert_called_once()
        _, bucket, key = mock_s3.upload_file.call_args.args
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(key, "runs/1.2.3/07-kgx.tar.gz")
        self.assertEqual(mock_s3.upload_file.call_args.kwargs["ExtraArgs"]["ACL"], "private")

    def test_noop_without_bucket(self):
        """No S3 call at all in local mode."""
        mock_s3 = self._call("")
        mock_s3.upload_file.assert_not_called()


class SyncGoldenDumpFromS3TestCase(unittest.TestCase):
    """Tests for the sync_golden_dump_from_s3 task."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.golden_dump_dir = Path(tmp.name) / "arangodump-golden-1.2.3"

    @staticmethod
    def _write_archive(b, k, dest):
        """Stand in for the download with a one-file dump archive."""
        payload = Path(dest).with_suffix(".json")
        payload.write_text("{}\n")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(payload, arcname="arangodump-golden-1.2.3/dump.json")
        payload.unlink()

    def _call(self, bucket, download=None):
        with patch("pipeline.get_run_logger", return_value=_noop_logger()), \
             patch("pipeline.S3_BUCKET", bucket), \
             patch("_common.S3_BUCKET", bucket), \
             patch("_common.boto3") as mock_boto3:
            mock_boto3.client.return_value.download_file.side_effect = (
                download or self._write_archive
            )
            sync_golden_dump_from_s3.fn(self.golden_dump_dir, "1.2.3")
        return mock_boto3.client.return_value

    def test_downloads_stage_06_key(self):
        """Fetches runs/{run}/06-golden-dump.tar.gz when missing locally."""
        mock_s3 = self._call("my-bucket")
        mock_s3.download_file.assert_called_once()
        bucket, key, _ = mock_s3.download_file.call_args.args
        self.assertEqual((bucket, key), ("my-bucket", "runs/1.2.3/06-golden-dump.tar.gz"))
        self.assertTrue((self.golden_dump_dir / "dump.json").is_file())

    def test_failed_download_leaves_no_dump_dir(self):
        """An interrupted or empty download leaves nothing a re-run would trust."""

        def fail(b, k, dest):
            raise OSError("connection reset")

        for download in (fail, lambda b, k, dest: tarfile.open(dest, "w:gz").close()):
            with self.assertRaises((OSError, RuntimeError)):
                self._call("my-bucket", download=download)
            self.assertEqual(list(self.golden_dump_dir.parent.iterdir()), [])

    def test_noop_when_present_locally(self):
        """Does not re-download a golden dump already on disk."""
        self.golden_dump_dir.mkdir()
        mock_s3 = self._call("my-bucket")
        mock_s3.download_file.assert_not_called()

    def test_noop_without_bucket(self):
        """No S3 call at all in local mode."""
        mock_s3 = self._call("")
        mock_s3.download_file.assert_not_called()


# Every task and side-effecting helper the flow body calls.
_FLOW_DEPS = [
    "ensure_jar",
    "stop_arangodb",
    "_wipe_arangodb_data",
    "start_arangodb",
    "_set_arango_port",
    "require_arangodb",
    "download_ontologies",
    "slim_ontologies",
    "build_ontology_graph",
    "dump_arangodb",
    "export_graphs_and_analyzers",
    "sync_baseline_dump_to_s3",
    "sync_baseline_dump_from_s3",
    "sync_results_from_s3",
    "sync_external_from_s3",
    "validate_release_dir",
    "validate_external_files",
    "restore_arangodb",
    "import_graphs_from_sidecar",
    "write_tuples",
    "sync_tuples_to_s3",
    "validate_tuple_files",
    "build_results_graph",
    "create_analyzers_and_views",
    "sync_results_dump_to_s3",
    "sync_results_dump_from_s3",
    "build_induced_subgraph",
    "promote_to_production",
    "export_kgx",
    "sync_kgx_to_s3",
    "sync_golden_dump_from_s3",
    "post_github_deployment_status",
    "_get_or_create_arango_password",
]


class FlowKgxMatrixTestCase(unittest.TestCase):
    """nlm_ckn_etl flag matrix for the KGX export."""

    RUN = "1.2.3"
    JAR_KEY = "abcdef0123456789"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo_root = Path(tmp.name)
        data = self.repo_root / "data"
        self.results_dump_dir = data / f"arangodump-results-{self.JAR_KEY}-{self.RUN}"
        self.golden_dump_dir = data / f"arangodump-golden-{self.RUN}"
        self.kgx_dir = data / f"kgx-{self.RUN}"
        self.calls = []
        self.logger = _noop_logger()
        self.logger.warning = MagicMock()

    def _run(self, side_effects=None, **flags):
        """Run the flow body with every task mocked; return {name: mock}."""
        returns = {
            "ensure_jar": self.JAR_KEY,
            "start_arangodb": 54321,
            "_get_or_create_arango_password": "secret",
        }
        side_effects = side_effects or {}
        mocks = {}
        with ExitStack() as stack:
            stack.enter_context(patch("pipeline.get_run_logger", return_value=self.logger))
            stack.enter_context(patch("pipeline.REPO_ROOT", self.repo_root))
            stack.enter_context(patch("pipeline.ARANGO_DB_IS_LOCAL", True))
            for name in _FLOW_DEPS:

                def effect(*a, _name=name, **kw):
                    self.calls.append(_name)
                    if _name in side_effects:
                        side_effects[_name](*a, **kw)
                    return returns.get(_name)

                mocks[name] = stack.enter_context(
                    patch(f"pipeline.{name}", side_effect=effect)
                )
            nlm_ckn_etl.fn(run_name=self.RUN, **flags)
        return mocks

    def _assert_no_phase_work(self):
        for name in (
            "download_ontologies",
            "write_tuples",
            "build_results_graph",
            "build_induced_subgraph",
            "dump_arangodb",
            "promote_to_production",
        ):
            self.assertNotIn(name, self.calls, f"{name} must not run")

    def test_force_kgx_alone_restores_golden_and_exports(self):
        """--force-kgx alone: fresh ArangoDB, restore golden, export, upload."""
        self.golden_dump_dir.mkdir(parents=True)
        mocks = self._run(force_kgx=True)

        self._assert_no_phase_work()
        # Standalone export reads only the golden dump: no Maven, no JAR download.
        mocks["ensure_jar"].assert_not_called()
        mocks["start_arangodb"].assert_called_once()
        mocks["restore_arangodb"].assert_called_once_with(self.golden_dump_dir, "secret")
        mocks["export_kgx"].assert_called_once_with(self.kgx_dir, "secret")
        mocks["sync_kgx_to_s3"].assert_called_once_with(self.kgx_dir, run_name=self.RUN)
        # A plain restore: named graphs and views are not needed for export.
        mocks["import_graphs_from_sidecar"].assert_not_called()
        mocks["create_analyzers_and_views"].assert_not_called()
        self.assertLess(self.calls.index("restore_arangodb"), self.calls.index("export_kgx"))
        self.assertLess(self.calls.index("export_kgx"), self.calls.index("sync_kgx_to_s3"))

    def test_force_kgx_pulls_golden_dump_from_s3(self):
        """--force-kgx alone with no local golden dump fetches it first."""
        mocks = self._run(
            side_effects={
                "sync_golden_dump_from_s3": lambda d, run: Path(d).mkdir(parents=True)
            },
            force_kgx=True,
        )
        mocks["sync_golden_dump_from_s3"].assert_called_once_with(
            self.golden_dump_dir, self.RUN
        )
        mocks["export_kgx"].assert_called_once()

    def test_force_kgx_without_golden_dump_raises(self):
        """--force-kgx alone fails loudly when no golden dump can be found."""
        with self.assertRaisesRegex(RuntimeError, "Golden dump not found"):
            self._run(force_kgx=True)
        self.assertNotIn("export_kgx", self.calls)

    def test_force_kgx_when_golden_exists_and_archive_skipped(self):
        """--run-archive skipped by an existing golden dump still exports under -K."""
        self.golden_dump_dir.mkdir(parents=True)
        mocks = self._run(run_archive=True, force_kgx=True)
        self._assert_no_phase_work()
        mocks["ensure_jar"].assert_not_called()
        mocks["export_kgx"].assert_called_once_with(self.kgx_dir, "secret")

    def test_run_archive_exports_inside_phase3(self):
        """--run-archive alone: export happens in Phase 3, no golden restore."""
        self.results_dump_dir.mkdir(parents=True)
        mocks = self._run(run_archive=True)

        # Phase 3 restores the JAR-keyed results dump, so it resolves the JAR.
        mocks["ensure_jar"].assert_called_once()
        mocks["export_kgx"].assert_called_once_with(self.kgx_dir, "secret")
        mocks["sync_kgx_to_s3"].assert_called_once_with(self.kgx_dir, run_name=self.RUN)
        mocks["sync_golden_dump_from_s3"].assert_not_called()
        mocks["restore_arangodb"].assert_called_once_with(self.results_dump_dir, "secret")
        # Exported from the golden state, uploaded before promotion.
        self.assertLess(self.calls.index("dump_arangodb"), self.calls.index("export_kgx"))
        self.assertLess(
            self.calls.index("sync_kgx_to_s3"), self.calls.index("promote_to_production")
        )

    def test_run_archive_with_force_kgx_exports_once(self):
        """When Phase 3 runs, -K does not trigger a second export."""
        self.results_dump_dir.mkdir(parents=True)
        mocks = self._run(run_archive=True, force_kgx=True)
        mocks["export_kgx"].assert_called_once()
        mocks["sync_kgx_to_s3"].assert_called_once()
        mocks["sync_golden_dump_from_s3"].assert_not_called()

    def test_skipped_archive_without_force_kgx_exports_nothing(self):
        """An existing golden dump skips Phase 3 and, without -K, the export too."""
        self.golden_dump_dir.mkdir(parents=True)
        mocks = self._run(run_archive=True)
        mocks["export_kgx"].assert_not_called()
        mocks["sync_kgx_to_s3"].assert_not_called()

    def test_no_flags_warns_and_returns(self):
        """No flags at all: warn (naming force_kgx) and do nothing."""
        mocks = self._run()
        self.assertEqual(self.calls, [])
        mocks["ensure_jar"].assert_not_called()
        self.logger.warning.assert_called_once()
        self.assertIn("force_kgx", self.logger.warning.call_args.args[0])


class ForceKgxCliTestCase(unittest.TestCase):
    """The -K/--force-kgx flag is registered without colliding with others."""

    def test_help_lists_force_kgx(self):
        result = subprocess.run(
            [sys.executable, str(_SRC / "flows" / "pipeline.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-K, --force-kgx", result.stdout)


if __name__ == "__main__":
    unittest.main()
