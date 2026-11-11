import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

import ProductionDataSpecification as spec
import ProductionDataValidator as v


class SpecConsistencyTestCase(unittest.TestCase):
    """The spec must stay the single source of truth for the readers."""

    def test_min_cluster_size_single_sourced(self):
        import LoaderUtilities

        self.assertEqual(LoaderUtilities.MIN_CLUSTER_SIZE, spec.MIN_CLUSTER_SIZE)

    def test_obo_purl_to_curie_matches_reader(self):
        from TupleWriterUtilities import purl_to_curie

        for s in (
            "http://purl.obolibrary.org/obo/CL_0000235",
            "https://purl.obolibrary.org/obo/UBERON_0002048",
            "CL:0000235",
            "not-a-purl",
        ):
            self.assertEqual(spec.obo_purl_to_curie(s), purl_to_curie(s))

    def test_companion_basename(self):
        ns = "results_ensg_liver_Foo_2020_ct_X_umap_ab12cd.csv"
        self.assertEqual(
            spec.companion_basename(ns, spec.SUMMARY_PREFIX),
            "master_dataset_summary_liver_Foo_2020_ct_X_umap_ab12cd.csv",
        )


@dataclass
class Tree:
    """Every path the fixture builder writes, so tests mutate by name.

    Keeping the directory layout in one place (the builder) means a layout
    change touches only :meth:`ValidatorFixtureTestCase._build`, not the
    dozen tests that previously re-derived companion paths by hand.
    """

    root: Path
    stem: str
    name: str  # results_ensg_*.csv basename
    ds: Path  # dataset dir holding results + companions
    results: Path
    summary: Path
    silhouette: Path
    mapping: Path
    harvester: Path
    uberon: Path
    manifest: Path


class ValidatorFixtureTestCase(unittest.TestCase):
    """Each check fires on a purpose-built violation and stays quiet on a
    well-formed tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tree = self._build(self.root)

    def _write(self, path, df):
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)

    def _rel(self, path):
        """Path relative to the fixture root, matching validator messages."""
        return str(Path(path).relative_to(self.root))

    def _find(self, report, check):
        """All findings (any severity) carrying *check*."""
        return [f for f in report.findings if f.check == check]

    def _build(self, root) -> Tree:
        """Build a minimal, conformant dataset under *root* and return its paths."""
        stem = "liver_Foo_2020_ct_X_umap_ab12cd"
        name = f"results_ensg_{stem}.csv"
        ds = root / "liver" / "results" / "liver-Foo-2020"
        results = ds / name
        summary = ds / f"master_dataset_summary_{stem}.csv"
        silhouette = ds / f"silhouette_fscore_summary_{stem}.csv"
        mapping = root / "liver" / "reference" / f"cluster_cid_mapping_{stem}.csv"
        harvester = (
            root / "liver" / "cellxgene-harvester" / "homo_sapiens_liver_harvester_final.csv"
        )
        uberon = root / "liver" / "cellxgene-harvester" / "uberon_liver.csv"
        manifest = ds / "master_s3_manifest.csv"

        self._write(
            results,
            pd.DataFrame(
                {
                    "cluster_header": ["ct", "ct"],
                    "clusterName": ["A", "B"],
                    "clusterSize": [50, 60],
                    "f_score": [0.9, 0.8],
                    "NSForest_markers": ["['ENSG1']", "['ENSG2']"],
                    "binary_genes": ["['ENSG1']", "['ENSG2']"],
                }
            ),
        )
        self._write(
            summary,
            pd.DataFrame({"dataset_version_id": ["dv-1"], "organ": ["liver"]}),
        )
        self._write(
            silhouette,
            pd.DataFrame(
                {
                    "ct": ["A", "B"],
                    "mean": [0.1, 0.2],
                    "std": [0.0, 0.0],
                    "median": [0.1, 0.2],
                    "count": [50, 60],
                    "q1": [0.0, 0.1],
                    "q3": [0.2, 0.3],
                    "f_score": [0.9, 0.8],
                }
            ),
        )
        self._write(
            mapping,
            pd.DataFrame(
                {
                    "cluster_name": ["A", "B"],
                    "skos": ["exactMatch", "exactMatch"],
                    "manual_mapped_cid": ["CL:0000235", "CL:0000236"],
                    "cell_ontology_id": ["CL:0000235", "CL:0000236"],
                }
            ),
        )
        self._write(harvester, pd.DataFrame({"dataset_version_id": ["dv-1"]}))
        self._write(
            uberon,
            pd.DataFrame(
                {
                    "obo_id": ["UBERON:0002107", "UBERON:0001280"],
                    "label": ["liver", "caudate lobe of liver"],
                    "level": ["root", "descendant"],
                }
            ),
        )
        self._write(
            manifest,
            pd.DataFrame({"filename": [name], "s3_path": ["s3://x/y"]}),
        )
        return Tree(
            root=root,
            stem=stem,
            name=name,
            ds=ds,
            results=results,
            summary=summary,
            silhouette=silhouette,
            mapping=mapping,
            harvester=harvester,
            uberon=uberon,
            manifest=manifest,
        )

    def test_good_tree_is_clean(self):
        report = v.validate(self.root)
        self.assertEqual(report.errors(), [], msg=[vars(f) for f in report.errors()])
        self.assertEqual(
            report.warnings(), [], msg=[vars(f) for f in report.warnings()]
        )

    def test_uberon_without_a_root_term(self):
        df = pd.read_csv(self.tree.uberon)
        df["level"] = "descendant"
        df.to_csv(self.tree.uberon, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "uberon-root")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.uberon))

    def test_organ_without_a_uberon_file(self):
        self.tree.uberon.unlink()
        report = v.validate(self.root)
        findings = self._find(report, "uberon-root-missing")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.summary))

    def test_no_results(self):
        # The one case that needs an empty tree rather than the fixture.
        with tempfile.TemporaryDirectory() as empty:
            report = v.validate(Path(empty))
            self.assertTrue(any(f.check == "no-results" for f in report.errors()))

    def test_missing_summary_companion(self):
        self.tree.summary.unlink()
        report = v.validate(self.root)
        findings = self._find(report, "companion-missing")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.results))

    def test_cluster_header_missing_with_silhouette(self):
        df = pd.read_csv(self.tree.results).drop(columns=["cluster_header"])
        df.to_csv(self.tree.results, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "cluster-header-missing")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.results))

    def test_silhouette_join_column_mismatch(self):
        df = pd.read_csv(self.tree.silhouette).rename(columns={"ct": "WRONG"})
        df.to_csv(self.tree.silhouette, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "silhouette-join-column")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.silhouette))

    def test_flatten_collision(self):
        # Same results basename in a second dataset dir → collision.
        dup = self.root / "liver" / "results" / "liver-Foo-2020-DUP" / self.tree.name
        self._write(dup, pd.read_csv(self.tree.results))
        report = v.validate(self.root)
        findings = self._find(report, "flatten-collision")
        self.assertEqual(len(findings), 1)
        self.assertIn(self.tree.name, findings[0].message)

    def test_companion_ambiguous_warns(self):
        # A second summary with the same basename in another dir makes the
        # companion glob return two matches (flatten-collision co-fires).
        dup = self.root / "liver" / "results" / "liver-Foo-2020-DUP" / self.tree.summary.name
        self._write(dup, pd.read_csv(self.tree.summary))
        report = v.validate(self.root)
        findings = self._find(report, "companion-ambiguous")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.results))
        self.assertIn("summary", findings[0].message)

    def test_manifest_missing_column(self):
        # Drop a required manifest column → manifest-columns ERROR.
        pd.DataFrame({"filename": [self.tree.name]}).to_csv(
            self.tree.manifest, index=False
        )
        report = v.validate(self.root)
        findings = self._find(report, "manifest-columns")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.manifest))
        self.assertIn("s3_path", findings[0].message)

    def test_manifest_missing_file(self):
        # A results file listed in the manifest but absent from the tree.
        ghost = "results_ensg_GONE_2020_ct_X_umap_ff00ff.csv"
        df = pd.read_csv(self.tree.manifest)
        df = pd.concat(
            [df, pd.DataFrame({"filename": [ghost], "s3_path": ["s3://x/z"]})],
            ignore_index=True,
        )
        df.to_csv(self.tree.manifest, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "manifest-missing-file")
        self.assertEqual(len(findings), 1)
        self.assertIn(ghost, findings[0].message)

    def test_harvester_missing_column(self):
        # Harvester without its join column → harvester-columns ERROR.
        pd.DataFrame({"other_col": ["x"]}).to_csv(self.tree.harvester, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "harvester-columns")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.harvester))
        self.assertIn("dataset_version_id", findings[0].message)

    def test_mapping_bad_cl_curie_warns(self):
        df = pd.read_csv(self.tree.mapping)
        df.loc[0, "manual_mapped_cid"] = "garbage"
        df.loc[0, "cell_ontology_id"] = "garbage"
        df.to_csv(self.tree.mapping, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "mapping-cl-curie")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.mapping))

    # --- P1: value-format checks -------------------------------------------

    def test_gene_list_unparseable(self):
        df = pd.read_csv(self.tree.results)
        df.loc[0, "NSForest_markers"] = "not-a-list"
        df.to_csv(self.tree.results, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "gene-list-unparseable")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.results))

    def test_clustersize_nonnumeric(self):
        df = pd.read_csv(self.tree.results)
        df["clusterSize"] = df["clusterSize"].astype(object)
        df.loc[0, "clusterSize"] = "big"
        df.to_csv(self.tree.results, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "clustersize-nonnumeric")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.results))

    def test_small_cluster_bad_gene_list_ignored(self):
        """A malformed gene list on a SMALL cluster is not parsed downstream."""
        df = pd.read_csv(self.tree.results)
        df.loc[0, "clusterSize"] = 1  # below MIN_CLUSTER_SIZE
        df.loc[0, "NSForest_markers"] = "not-a-list"
        df.to_csv(self.tree.results, index=False)
        report = v.validate(self.root)
        self.assertFalse(
            any(f.check == "gene-list-unparseable" for f in report.errors())
        )

    # --- P1: cross-file referential integrity ------------------------------

    def test_tissue_curie_warns(self):
        df = pd.read_csv(self.tree.summary)
        df["tissue_ontology_term_id"] = ["liver"]  # not a CURIE
        df.to_csv(self.tree.summary, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "tissue-curie")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.summary))

    def test_harvester_dvid_missing_warns(self):
        pd.DataFrame({"dataset_version_id": ["other-dv"]}).to_csv(
            self.tree.harvester, index=False
        )
        report = v.validate(self.root)
        findings = self._find(report, "harvester-dvid-missing")
        self.assertEqual(len(findings), 1)

    def test_mapping_cluster_orphan_warns(self):
        df = pd.read_csv(self.tree.mapping)
        df.loc[0, "cluster_name"] = "ZZZ-not-in-results"
        df.to_csv(self.tree.mapping, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "mapping-cluster-orphan")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.mapping))

    def test_mapping_dvid_orphan_warns(self):
        df = pd.read_csv(self.tree.mapping)
        df["dataset_version_id"] = ["dv-1", "dv-UNKNOWN"]
        df.to_csv(self.tree.mapping, index=False)
        report = v.validate(self.root)
        findings = self._find(report, "mapping-dvid-orphan")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, self._rel(self.tree.mapping))

    # --- P1: multi-dataset membership invariant ----------------------------

    def _multi_dataset_tree(self, root):
        """A conformant multi-dataset (Jorstad-like) dataset under *root*:
        2 datasets, a mapping resolving each cluster to one of them."""
        stem = "neocortex_Jorstad_2023_X_umap_aa11bb"
        ds = root / "neocortex" / "results" / "neocortex-Jorstad-2023"
        self._write(
            ds / f"results_ensg_{stem}.csv",
            pd.DataFrame(
                {
                    "clusterName": ["A", "B"],
                    "clusterSize": [50, 60],
                    "f_score": [0.9, 0.8],
                    "NSForest_markers": ["['ENSG1']", "['ENSG2']"],
                    "binary_genes": ["['ENSG1']", "['ENSG2']"],
                }
            ),
        )
        self._write(
            ds / f"master_dataset_summary_{stem}.csv",
            pd.DataFrame({"dataset_version_id": ["dv-1", "dv-2"]}),
        )
        self._write(
            root / "neocortex" / "reference" / f"cluster_cid_mapping_{stem}.csv",
            pd.DataFrame(
                {
                    "dataset_version_id": ["dv-1", "dv-2"],
                    "cluster_name": ["A", "B"],
                    "skos": ["exactMatch", "exactMatch"],
                    "manual_mapped_cid": ["CL:0000235", "CL:0000236"],
                    "cell_ontology_id": ["CL:0000235", "CL:0000236"],
                }
            ),
        )
        self._write(
            root / "neocortex" / "cellxgene-harvester" / "homo_sapiens_neocortex_harvester_final.csv",
            pd.DataFrame({"dataset_version_id": ["dv-1", "dv-2"]}),
        )
        return stem, ds

    def test_multidataset_good_has_no_errors(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._multi_dataset_tree(root)
            report = v.validate(root)
            self.assertEqual(report.errors(), [], msg=[vars(f) for f in report.errors()])

    def test_multidataset_no_mapping(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stem, _ = self._multi_dataset_tree(root)
            (root / "neocortex" / "reference" / f"cluster_cid_mapping_{stem}.csv").unlink()
            report = v.validate(root)
            self.assertTrue(
                any(f.check == "multidataset-no-mapping" for f in report.errors())
            )

    def test_multidataset_mapping_no_dvid_column(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stem, _ = self._multi_dataset_tree(root)
            mp = root / "neocortex" / "reference" / f"cluster_cid_mapping_{stem}.csv"
            pd.read_csv(mp).drop(columns=["dataset_version_id"]).to_csv(mp, index=False)
            report = v.validate(root)
            self.assertTrue(
                any(f.check == "multidataset-mapping-no-dvid" for f in report.errors())
            )

    def test_multidataset_mapping_no_cluster_name_column(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stem, _ = self._multi_dataset_tree(root)
            mp = root / "neocortex" / "reference" / f"cluster_cid_mapping_{stem}.csv"
            pd.read_csv(mp).drop(columns=["cluster_name"]).to_csv(mp, index=False)
            report = v.validate(root)
            self.assertTrue(
                any(
                    f.check == "multidataset-mapping-no-cluster-name"
                    for f in report.errors()
                )
            )

    def test_multidataset_cluster_unresolved(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stem, _ = self._multi_dataset_tree(root)
            mp = root / "neocortex" / "reference" / f"cluster_cid_mapping_{stem}.csv"
            df = pd.read_csv(mp)
            df.loc[1, "cluster_name"] = "ZZZ"  # cluster B no longer resolved
            df.to_csv(mp, index=False)
            report = v.validate(root)
            self.assertTrue(
                any(f.check == "multidataset-cluster-unresolved" for f in report.errors())
            )

    # -- GitHub annotations ---------------------------------------------------

    def _run_main(self, data_dir, env, *extra):
        """Run the CLI with *env* as the only GitHub variables set.

        Returns ``(exit_code, stdout, stderr)``.
        """
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ):
            for key in ("GITHUB_ACTIONS", "GITHUB_WORKSPACE"):
                os.environ.pop(key, None)
            os.environ.update(env)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = v.main(["--data-dir", str(data_dir), *extra])
        return rc, out.getvalue(), err.getvalue()

    def _workspace_tree(self):
        """Build the fixture at ``<workspace>/data/prod`` with one ERROR."""
        workspace = self.root / "ws"
        tree = self._build(workspace / "data" / "prod")
        tree.summary.unlink()  # -> companion-missing on the results file
        return workspace, tree

    def test_annotations_on_stderr_under_github_actions(self):
        workspace, tree = self._workspace_tree()
        env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(workspace)}
        rc, out, err = self._run_main(tree.root, env)
        rc0, out0, err0 = self._run_main(tree.root, {})
        self.assertEqual((rc, out), (rc0, out0))  # stdout and exit unchanged
        self.assertEqual(rc, 1)
        self.assertNotIn("::", out)
        rel = tree.results.relative_to(workspace).as_posix()
        self.assertTrue(
            any(
                line.startswith(f"::error file={rel},title=companion-missing::")
                for line in err.splitlines()
            ),
            err,
        )
        self.assertTrue(rel.startswith("data/prod/"))

    def test_annotations_keep_json_stdout(self):
        workspace, tree = self._workspace_tree()
        env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(workspace)}
        _, out, err = self._run_main(tree.root, env, "--json")
        self.assertEqual(json.loads(out)["errors"], 1)
        self.assertIn("::error ", err)

    def test_no_annotations_without_github_actions(self):
        workspace, tree = self._workspace_tree()
        _, _, err = self._run_main(tree.root, {"GITHUB_WORKSPACE": str(workspace)})
        self.assertEqual(err, "")

    def test_no_annotations_flag(self):
        workspace, tree = self._workspace_tree()
        env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(workspace)}
        _, _, err = self._run_main(tree.root, env, "--no-annotations")
        self.assertEqual(err, "")

    def test_annotations_omit_file_outside_workspace(self):
        _, tree = self._workspace_tree()
        with tempfile.TemporaryDirectory() as other:
            env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": other}
            _, _, err = self._run_main(tree.root, env)
        self.assertIn("::error title=companion-missing::", err)
        self.assertNotIn("file=", err)


class AnnotationHelpersTestCase(unittest.TestCase):
    """Prefix derivation and workflow-command escaping."""

    def test_workspace_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.assertEqual(v._workspace_prefix(ws / "data" / "prod", ws), "data/prod/")
            self.assertEqual(v._workspace_prefix(ws, ws), "")
            self.assertIsNone(v._workspace_prefix(ws / "x", None))
            self.assertIsNone(v._workspace_prefix(ws / "x", ""))
            self.assertIsNone(v._workspace_prefix(ws.parent, ws))

    def test_escape(self):
        self.assertEqual(v._escape("50% a:b,c\r\nd"), "50%25 a:b,c%0D%0Ad")
        self.assertEqual(
            v._escape("50% a:b,c\r\nd", prop=True), "50%25 a%3Ab%2Cc%0D%0Ad"
        )

    def test_annotation_line_escaped(self):
        report = v.Report(data_dir=Path("."))
        report.add(v.WARN, "chk", "bad: 1,2\nnext", "a,b:c.csv")
        report.add(v.ERROR, "nopath", "msg")
        buf = io.StringIO()
        v._print_annotations(report, "data/prod/", buf)
        self.assertEqual(
            buf.getvalue().splitlines(),
            [
                "::warning file=data/prod/a%2Cb%3Ac.csv,title=chk::bad: 1,2%0Anext",
                "::error title=nopath::msg",
            ],
        )


if __name__ == "__main__":
    unittest.main()
