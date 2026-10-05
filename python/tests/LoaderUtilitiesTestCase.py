from pathlib import Path
import string
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

import LoaderUtilities as lu


class LoaderUtilitiesTestCase(unittest.TestCase):
    """Pure unit tests for LoaderUtilities functions."""

    # get_uuid tests

    def test_get_uuid_length(self):
        """Returns a 12-character string."""
        uuid = lu.get_uuid()
        self.assertEqual(len(uuid), 12)

    def test_get_uuid_characters(self):
        """All characters are lowercase alphanumeric."""
        uuid = lu.get_uuid()
        allowed = set(string.ascii_lowercase + string.digits)
        for c in uuid:
            self.assertIn(c, allowed)

    def test_get_uuid_uniqueness(self):
        """Two calls return different values."""
        self.assertNotEqual(lu.get_uuid(), lu.get_uuid())

    # hyphenate tests

    def test_hyphenate_space(self):
        """Replaces space with hyphen."""
        self.assertEqual(lu.hyphenate("hello world"), "hello-world")

    def test_hyphenate_space(self):
        """Replaces spaces with hyphen."""
        self.assertEqual(lu.hyphenate("hello   world"), "hello-world")

    def test_hyphenate_underscore(self):
        """Replaces underscore with hyphen."""
        self.assertEqual(lu.hyphenate("hello_world"), "hello-world")

    def test_hyphenate_underscore(self):
        """Replaces underscores with hyphen."""
        self.assertEqual(lu.hyphenate("hello___world"), "hello-world")

    def test_hyphenate_comma(self):
        """Replaces commas with hyphen."""
        self.assertEqual(lu.hyphenate("hello,world"), "hello-world")

    def test_hyphenate_comma(self):
        """Replaces commas with hyphens."""
        self.assertEqual(lu.hyphenate("hello,,,world"), "hello-world")

    def test_hyphenate_slash(self):
        """Replaces forward slashe with hyphen."""
        self.assertEqual(lu.hyphenate("hello/world"), "hello-world")

    def test_hyphenate_slash(self):
        """Replaces forward slashes with hyphen."""
        self.assertEqual(lu.hyphenate("hello///world"), "hello-world")

    def test_hyphenate_multiple_separators(self):
        """Handles multiple different separators."""
        self.assertEqual(lu.hyphenate("a b_c/d"), "a-b-c-d")

    # get_value_or_none tests

    def test_get_value_or_none_nested(self):
        """Accesses nested dict value."""
        data = {"a": {"b": {"c": 42}}}
        self.assertEqual(lu.get_value_or_none(data, ["a", "b", "c"]), 42)

    def test_get_value_or_none_partial(self):
        """Returns intermediate dict."""
        data = {"a": {"b": {"c": 42}}}
        self.assertEqual(lu.get_value_or_none(data, ["a", "b"]), {"c": 42})

    def test_get_value_or_none_missing_key(self):
        """Returns None for missing key."""
        data = {"a": {"b": {"c": 42}}}
        self.assertIsNone(lu.get_value_or_none(data, ["a", "x"]))

    def test_get_value_or_none_missing_first_key(self):
        """Returns None for missing first key."""
        data = {"a": 1}
        self.assertIsNone(lu.get_value_or_none(data, ["x"]))

    def test_get_value_or_none_empty_dict(self):
        """Returns None for empty dict."""
        self.assertIsNone(lu.get_value_or_none({}, ["a"]))

    # get_values_or_none tests

    def test_get_values_or_none_collects(self):
        """Collects comma-separated values from list items."""
        data = {"items": [{"name": "Alice"}, {"name": "Bob"}]}
        self.assertEqual(lu.get_values_or_none(data, "items", ["name"]), "Alice, Bob")

    def test_get_values_or_none_missing_key(self):
        """Returns empty string for missing list key."""
        data = {"items": [{"name": "Alice"}]}
        self.assertEqual(lu.get_values_or_none(data, "missing", ["name"]), "")

    def test_get_values_or_none_single_item(self):
        """Single item returns just that value."""
        data = {"items": [{"name": "Alice"}]}
        self.assertEqual(lu.get_values_or_none(data, "items", ["name"]), "Alice")

    # map_gene_name_to_ensembl_ids tests

    def test_map_gene_name_to_ensembl_ids_single(self):
        """Maps gene name to single Ensembl id."""

        df = pd.DataFrame(
            {
                "external_gene_name": ["BRCA1"],
                "ensembl_gene_id": ["ENSG00000012048"],
            }
        ).set_index("external_gene_name")
        ids = lu.map_gene_name_to_ensembl_ids("BRCA1", df)
        self.assertEqual(ids, ["ENSG00000012048"])

    def test_map_gene_name_to_ensembl_ids_multiple(self):
        """Maps gene name to multiple Ensembl ids."""

        df = pd.DataFrame(
            {
                "external_gene_name": ["TP53", "TP53"],
                "ensembl_gene_id": ["ENSG00000141510", "ENSG00000999999"],
            }
        ).set_index("external_gene_name")
        ids = lu.map_gene_name_to_ensembl_ids("TP53", df)
        self.assertIsInstance(ids, list)
        self.assertIn("ENSG00000141510", ids)
        self.assertIn("ENSG00000999999", ids)

    def test_map_gene_name_to_ensembl_ids_missing(self):
        """Returns empty list for unknown gene name."""

        df = pd.DataFrame(
            {
                "external_gene_name": ["BRCA1"],
                "ensembl_gene_id": ["ENSG00000012048"],
            }
        ).set_index("external_gene_name")
        ids = lu.map_gene_name_to_ensembl_ids("NONEXISTENT", df)
        self.assertEqual(ids, [])

    # map_gene_ensembl_id_to_names tests

    def test_map_gene_ensembl_id_to_names(self):
        """Maps Ensembl id to single gene name."""

        df = pd.DataFrame(
            {
                "ensembl_gene_id": ["ENSG00000012048"],
                "external_gene_name": ["BRCA1"],
            }
        ).set_index("ensembl_gene_id")
        names = lu.map_gene_ensembl_id_to_names("ENSG00000012048", df)
        self.assertEqual(names, ["BRCA1"])

    def test_map_gene_ensembl_id_to_names(self):
        """Maps Ensembl id to multiple gene names."""

        df = pd.DataFrame(
            {
                "ensembl_gene_id": ["ENSG00000012048", "ENSG00000012048"],
                "external_gene_name": ["BRCA1", "BRCA9"],
            }
        ).set_index("ensembl_gene_id")
        names = lu.map_gene_ensembl_id_to_names("ENSG00000012048", df)
        self.assertIsInstance(names, list)
        self.assertIn("BRCA1", names)
        self.assertIn("BRCA9", names)

    def test_map_gene_ensembl_id_to_names_missing(self):
        """Returns empty list for unknown Ensembl id."""

        df = pd.DataFrame(
            {
                "ensembl_gene_id": ["ENSG00000012048"],
                "external_gene_name": ["BRCA1"],
            }
        ).set_index("ensembl_gene_id")
        names = lu.map_gene_ensembl_id_to_names("ENSG99999999999", df)
        self.assertEqual(names, [])

    # map_gene_name_to_entrez_ids tests

    def test_map_gene_name_to_entrez_ids(self):
        """Maps gene name to single Entrez id."""

        df = pd.DataFrame(
            {
                "external_gene_name": ["BRCA1"],
                "entrezgene_id": ["672"],
            }
        ).set_index("external_gene_name")
        ids = lu.map_gene_name_to_entrez_ids("BRCA1", df)
        self.assertEqual(ids, ["672"])

    def test_map_gene_name_to_entrez_ids(self):
        """Maps gene name to multiple Entrez ids."""

        df = pd.DataFrame(
            {
                "external_gene_name": ["BRCA1", "BRCA1"],
                "entrezgene_id": ["672", "999"],
            }
        ).set_index("external_gene_name")
        ids = lu.map_gene_name_to_entrez_ids("BRCA1", df)
        self.assertIsInstance(ids, list)
        self.assertIn("672", ids)
        self.assertIn("999", ids)

    def test_map_gene_name_to_entrez_ids_missing(self):
        """Returns empty list for unknown gene name."""

        df = pd.DataFrame(
            {
                "external_gene_name": ["BRCA1"],
                "entrezgene_id": ["672"],
            }
        ).set_index("external_gene_name")
        ids = lu.map_gene_name_to_entrez_ids("NONEXISTENT", df)
        self.assertEqual(ids, [])

    # map_gene_entrez_id_to_names tests

    def test_map_gene_entrez_id_to_names(self):
        """Maps Entrez id to single gene name."""

        df = pd.DataFrame(
            {
                "entrezgene_id": ["672"],
                "external_gene_name": ["BRCA1"],
            }
        ).set_index("entrezgene_id")
        names = lu.map_gene_entrez_id_to_names("672", df)
        self.assertEqual(names, ["BRCA1"])

    def test_map_gene_entrez_id_to_names(self):
        """Maps Entrez id to multiple gene names."""

        df = pd.DataFrame(
            {
                "entrezgene_id": ["672", "672"],
                "external_gene_name": ["BRCA1", "BRCA9"],
            }
        ).set_index("entrezgene_id")
        names = lu.map_gene_entrez_id_to_names("672", df)
        self.assertIsInstance(names, list)
        self.assertIn("BRCA1", names)
        self.assertIn("BRCA9", names)

    def test_map_gene_entrez_id_to_names_missing(self):
        """Returns empty list for unknown Entrez id."""

        df = pd.DataFrame(
            {
                "entrezgene_id": ["672"],
                "external_gene_name": ["BRCA1"],
            }
        ).set_index("entrezgene_id")
        names = lu.map_gene_entrez_id_to_names("99999", df)
        self.assertEqual(names, [])

    # map_protein_ensembl_id_to_accession tests

    def test_map_protein_ensembl_id_to_accession_single(self):
        """Maps Ensembl protein id to single accession."""
        ensp2accn = {"ENSP001": "P12345"}
        self.assertEqual(
            lu.map_protein_ensembl_id_to_accession("ENSP001", ensp2accn), "P12345"
        )

    def test_map_protein_ensembl_id_to_accession_list(self):
        """Maps Ensembl protein id to first of multiple accessions."""
        ensp2accn = {"ENSP001": ["P11111", "P22222"]}
        self.assertEqual(
            lu.map_protein_ensembl_id_to_accession("ENSP001", ensp2accn), "P11111"
        )

    def test_map_protein_ensembl_id_to_accession_missing(self):
        """Returns None for unknown protein id."""
        ensp2accn = {"ENSP001": "P12345"}
        self.assertIsNone(lu.map_protein_ensembl_id_to_accession("ENSP999", ensp2accn))

    # map_accession_to_protein_ensembl_id tests

    def test_map_accession_to_protein_ensembl_id_single(self):
        """Maps accession to single Ensembl protein id."""
        accn2ensp = {"P12345": "ENSP001"}
        self.assertEqual(
            lu.map_accession_to_protein_ensembl_id("P12345", accn2ensp), "ENSP001"
        )

    def test_map_accession_to_protein_ensembl_id_list(self):
        """Maps accession to first of multiple Ensembl protein ids."""
        accn2ensp = {"P12345": ["ENSP001", "ENSP002"]}
        self.assertEqual(
            lu.map_accession_to_protein_ensembl_id("P12345", accn2ensp), "ENSP001"
        )

    def test_map_accession_to_protein_ensembl_id_missing(self):
        """Returns None for unknown accession."""
        accn2ensp = {"P12345": "ENSP001"}
        self.assertIsNone(lu.map_accession_to_protein_ensembl_id("P99999", accn2ensp))

    # map_efo_to_mondo tests

    def test_map_efo_to_mondo(self):
        """Maps EFO term to MONDO term."""

        efo2mondo = pd.DataFrame(
            {"EFO": ["EFO_0000270"], "MONDO": ["MONDO_0004992"]}
        ).set_index("EFO")
        self.assertEqual(lu.map_efo_to_mondo("EFO_0000270", efo2mondo), "MONDO_0004992")

    def test_map_efo_to_mondo_missing(self):
        """Returns None for unknown EFO term."""

        efo2mondo = pd.DataFrame(
            {"EFO": ["EFO_0000270"], "MONDO": ["MONDO_0004992"]}
        ).set_index("EFO")
        self.assertIsNone(lu.map_efo_to_mondo("EFO_9999999", efo2mondo))

    # map_mesh_to_mondo tests

    def test_map_mesh_to_mondo(self):
        """Maps MeSH term to MONDO term."""
        mesh2mondo = {"MESH:D008264": "MONDO_0004992"}
        self.assertEqual(
            lu.map_mesh_to_mondo("MESH:D008264", mesh2mondo), "MONDO_0004992"
        )

    def test_map_mesh_to_mondo_missing(self):
        """Returns None for unknown MeSH term."""
        mesh2mondo = {"MESH:D008264": "MONDO_0004992"}
        self.assertIsNone(lu.map_mesh_to_mondo("MESH:D999999", mesh2mondo))

    # map_chembl_to_pubchem tests

    def test_map_chembl_to_pubchem_single(self):
        """Maps ChEMBL id to single PubChem id."""

        chembl2pubchem = pd.DataFrame(
            {"ChEMBL": ["CHEMBL25"], "PubChem": ["2244"]}
        ).set_index("ChEMBL")
        self.assertEqual(lu.map_chembl_to_pubchem("CHEMBL25", chembl2pubchem), "2244")

    def test_map_chembl_to_pubchem_list(self):
        """Maps ChEMBL id to first of multiple PubChem id."""

        chembl2pubchem = pd.DataFrame(
            {"ChEMBL": ["CHEMBL25", "CHEMBL25"], "PubChem": ["2244", "3344"]}
        ).set_index("ChEMBL")
        self.assertEqual(lu.map_chembl_to_pubchem("CHEMBL25", chembl2pubchem), "2244")

    def test_map_chembl_to_pubchem_missing(self):
        """Returns None for unknown ChEMBL id."""

        chembl2pubchem = pd.DataFrame(
            {"ChEMBL": ["CHEMBL25"], "PubChem": ["2244"]}
        ).set_index("ChEMBL")
        self.assertIsNone(lu.map_chembl_to_pubchem("CHEMBL99999", chembl2pubchem))

    # collect_unique_gene_names tests

    def test_collect_unique_gene_names(self):
        """Extracts unique gene names from NSForest markers and binary genes."""

        df = pd.DataFrame(
            {
                "clusterName": ["cluster1", "cluster2", "small_cluster"],
                "clusterSize": [100, 200, 5],
                "NSForest_markers": ["['TP53', 'BRCA1']", "['EGFR']", "['MYC']"],
                "binary_genes": ["['TP53', 'EGFR']", "['BRCA2']", "['KRAS']"],
            }
        )
        genes = lu.collect_unique_gene_names(df)
        self.assertIn("TP53", genes)
        self.assertIn("BRCA1", genes)
        self.assertIn("EGFR", genes)
        self.assertIn("BRCA2", genes)
        # Small cluster (size 5 < MIN_CLUSTER_SIZE=10) should be excluded
        self.assertNotIn("MYC", genes)
        self.assertNotIn("KRAS", genes)
        # Should be sorted
        self.assertEqual(genes, sorted(genes))

    # build_citation tests

    def test_build_citation(self):
        """Citation reads author, year, and journal."""
        self.assertEqual(
            lu.build_citation("Sikkema", 2023, "Nat Med"),
            "Sikkema (2023) Nat Med",
        )

    def test_build_citation_without_journal(self):
        """The journal is omitted when it is unknown."""
        self.assertEqual(lu.build_citation("Sikkema", 2023, None), "Sikkema (2023)")

    def test_build_citation_without_author_or_year(self):
        """A citation needs both an author and a year."""
        self.assertIsNone(lu.build_citation(None, 2023, "Nat Med"))
        self.assertIsNone(lu.build_citation("Sikkema", None, "Nat Med"))
        self.assertIsNone(lu.build_citation(None, None, None))

    def test_build_citation_reads_a_csv_row(self):
        """A missing CSV value reads as missing, not as "nan"."""
        row = pd.DataFrame(
            {"first_author": [None], "year": [None], "journal": [None]}
        ).iloc[0]
        self.assertIsNone(
            lu.build_citation(row.get("first_author"), row.get("year"), row.get("journal"))
        )

    def test_build_citation_reads_a_float_year(self):
        """A year pandas typed as a float reads as 2023, not as 2023.0."""
        self.assertEqual(
            lu.build_citation("Sikkema", 2023.0, "Nat Med"),
            "Sikkema (2023) Nat Med",
        )

    # build_dataset_label tests

    def test_build_dataset_label(self):
        """The label qualifies the citation with the dataset name."""
        self.assertEqual(
            lu.build_dataset_label("Sikkema (2023) Nat Med", "Lung, 3' v2"),
            "Sikkema (2023) Nat Med - Lung, 3' v2",
        )

    def test_build_dataset_label_falls_back(self):
        """The label is whichever part is known."""
        self.assertEqual(
            lu.build_dataset_label("Sikkema (2023) Nat Med", None),
            "Sikkema (2023) Nat Med",
        )
        self.assertEqual(lu.build_dataset_label(None, "Lung, 3' v2"), "Lung, 3' v2")
        self.assertIsNone(lu.build_dataset_label(None, None))


class DatasetOrgansMapTestCase(unittest.TestCase):
    """Tests for get_dataset_organs_map."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = Path(self._tmp.name)
        # Two organ pipelines that filtered the SAME source dataset (dvid-shared),
        # plus one organ-only dataset (dvid-kidney).  A companion results_ensg
        # file per summary is needed for get_dataset_file_paths to pair them.
        for suffix, rows in {
            "kidney_set": [("dvid-shared", "kidney"), ("dvid-kidney", "kidney")],
            "liver_set": [("dvid-shared", "liver")],
        }.items():
            (self.results_dir / f"results_ensg_{suffix}.csv").write_text(
                "clusterName\nc1\n"
            )
            lines = ["organ,dataset_version_id"]
            lines += [f"{organ},{dvid}" for dvid, organ in rows]
            (self.results_dir / f"master_dataset_summary_{suffix}.csv").write_text(
                "\n".join(lines) + "\n"
            )

    def tearDown(self):
        self._tmp.cleanup()

    def test_maps_source_dataset_to_all_its_organs(self):
        organs = lu.get_dataset_organs_map(self.results_dir)
        self.assertEqual(organs["dvid-shared"], {"kidney", "liver"})
        self.assertEqual(organs["dvid-kidney"], {"kidney"})


class ReferenceDatasetVersionIdsTestCase(unittest.TestCase):
    """Tests for get_reference_dataset_version_ids.

    A reference dataset is one with a cluster_cid_mapping companion file;
    the union of every such file's dataset_version_id column is the
    reference set (CellSetDataset.is_reference_dataset,
    Springbok-LLC/nlm-ckn-etl#122).
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_mapping(self, name, dvids):
        lines = ["dataset_version_id,cluster_name"] + [
            f"{dvid},c1" for dvid in dvids
        ]
        (self.results_dir / f"cluster_cid_mapping_{name}.csv").write_text(
            "\n".join(lines) + "\n"
        )

    def test_unions_dataset_version_ids_across_mapping_files(self):
        self._write_mapping("kidney_set", ["dvid-kidney"])
        self._write_mapping("liver_set", ["dvid-liver-a", "dvid-liver-b"])
        reference_ids = lu.get_reference_dataset_version_ids(self.results_dir)
        self.assertEqual(
            reference_ids, {"dvid-kidney", "dvid-liver-a", "dvid-liver-b"}
        )

    def test_dataset_with_no_mapping_file_is_not_reference(self):
        self._write_mapping("kidney_set", ["dvid-kidney"])
        reference_ids = lu.get_reference_dataset_version_ids(self.results_dir)
        self.assertNotIn("dvid-not-mapped", reference_ids)

    def test_no_mapping_files_gives_empty_set(self):
        reference_ids = lu.get_reference_dataset_version_ids(self.results_dir)
        self.assertEqual(reference_ids, set())

    def test_mapping_file_missing_dataset_version_id_column_is_skipped(self):
        (self.results_dir / "cluster_cid_mapping_broken.csv").write_text(
            "cluster_name\nc1\n"
        )
        self._write_mapping("kidney_set", ["dvid-kidney"])
        reference_ids = lu.get_reference_dataset_version_ids(self.results_dir)
        self.assertEqual(reference_ids, {"dvid-kidney"})


class HarvesterRowTestCase(unittest.TestCase):
    """A dataset takes its harvester row from its own organ's table.

    A source dataset harvested for several organs has one row per organ and
    those rows differ, so selecting on dataset_version_id alone attributed
    whichever table sorted first to all of them
    (Springbok-LLC/nlm-ckn-etl#63).
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = Path(self._tmp.name)
        for organ, rows in {
            "bone_marrow": [("dvid-shared", 20), ("dvid-marrow", 5)],
            "skin_of_body": [("dvid-shared", 8)],
        }.items():
            lines = ["dataset_version_id,donor_id_count"]
            lines += [f"{dvid},{count}" for dvid, count in rows]
            (
                self.results_dir / f"homo_sapiens_{organ}_harvester_final.csv"
            ).write_text("\n".join(lines) + "\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _data(self):
        return lu.get_cellxgene_harvester_data(self.results_dir)

    def test_organ_comes_from_the_filename(self):
        # The tables carry no organ column, so it can only come from there.
        self.assertEqual(
            set(self._data()["organ"]), {"bone_marrow", "skin_of_body"}
        )

    def test_multi_organ_dataset_takes_its_own_organs_row(self):
        data = self._data()
        self.assertEqual(
            lu.get_harvester_row(data, "dvid-shared", "skin of body")["donor_id_count"],
            8,
        )
        self.assertEqual(
            lu.get_harvester_row(data, "dvid-shared", "bone marrow")["donor_id_count"],
            20,
        )

    def test_single_organ_dataset_ignores_the_organ(self):
        # With one row there is nothing to tell apart, so a dataset whose
        # organ is spelled differently upstream still finds its row.
        row = lu.get_harvester_row(self._data(), "dvid-marrow", "something else")
        self.assertEqual(row["donor_id_count"], 5)

    def test_no_row_for_the_organ_yields_none(self):
        # An arbitrary organ's counts are worse than no counts.
        self.assertIsNone(lu.get_harvester_row(self._data(), "dvid-shared", "liver"))

    def test_unknown_dataset_yields_none(self):
        self.assertIsNone(lu.get_harvester_row(self._data(), "dvid-absent", "liver"))

    def test_empty_harvester_data_yields_none(self):
        self.assertIsNone(lu.get_harvester_row(pd.DataFrame(), "dvid-shared", "liver"))
        self.assertIsNone(lu.get_harvester_row(None, "dvid-shared", "liver"))


class BinaryScoresCompanionTestCase(unittest.TestCase):
    """Binary scores pair to a results file by prefix substitution.

    They are sparse in prod, so a results set without them must still be
    discovered rather than dropped.
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.results_dir = Path(self._tmp.name)
        for suffix in ("scored_set", "unscored_set"):
            (self.results_dir / f"results_ensg_{suffix}.csv").write_text(
                "clusterName\nc1\n"
            )
            (self.results_dir / f"master_dataset_summary_{suffix}.csv").write_text(
                "organ,dataset_version_id\nkidney,dvid-1\n"
            )
        (self.results_dir / "binary_scores_ensg_scored_set.csv").write_text(
            ",c1\nENSG00000141510,0.8\n"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_binary_scores_pair_to_their_results_file(self):
        paths = lu.get_dataset_file_paths(self.results_dir)
        found = dict(
            zip(
                (p.name for p in paths["nsforest_paths"]),
                paths["binary_scores_paths"],
            )
        )
        self.assertEqual(
            [p.name for p in found["results_ensg_scored_set.csv"]],
            ["binary_scores_ensg_scored_set.csv"],
        )

    def test_results_without_binary_scores_pair_to_nothing(self):
        paths = lu.get_dataset_file_paths(self.results_dir)
        found = dict(
            zip(
                (p.name for p in paths["nsforest_paths"]),
                paths["binary_scores_paths"],
            )
        )
        self.assertEqual(found["results_ensg_unscored_set.csv"], [])
