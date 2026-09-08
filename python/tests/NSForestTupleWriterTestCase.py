import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from NSForestTupleWriter import create_tuples, mean_binary_scores


class NSForestTupleWriterTestCase(unittest.TestCase):
    """Tests for NSForestTupleWriter.create_tuples."""

    def _make_data(self):
        nsforest = pd.DataFrame({
            "clusterName": ["T Cell"],
            "clusterSize": [1718],
            "f_score": [0.716],
            "precision": [0.787],
            "TN": [103482],
            "FP": [245],
            "FN": [813],
            "TP": [905],
            "marker_count": [1],
            "NSForest_markers": ["['TP53']"],
            "binary_genes": ["['TP53', 'BRCA1', 'EGFR']"],
            "uuid": ["abc123"],
        })
        summary = pd.DataFrame({
            "tissue_ontology_term_id": ["UBERON:0000966"],
        })
        return nsforest, summary

    def test_creates_tuples(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertGreater(len(tuples), 0)

    def test_contains_derives_from(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0001000" in p for p in preds))  # derives_from

    def test_contains_has_characterizing_marker_set(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0015004" in p for p in preds))

    def _part_of(self, tuples, object_prefix):
        """Return the subject terms of part_of triples with the given object."""
        # Gene part_of BMC and Gene part_of BGS share the part_of predicate,
        # so only the object tells them apart.
        return {
            str(t[0]).rsplit("/", 1)[-1]
            for t in tuples
            if len(t) == 3
            and "BFO_0000050" in str(t[1])
            and f"/{object_prefix}_" in str(t[2])
        }

    def test_contains_gene_part_of_bmc(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        # The marker genes alone, not every binary gene.
        self.assertEqual(self._part_of(tuples, "BMC"), {"GS_TP53"})

    def test_contains_gene_part_of_bgs(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertEqual(
            self._part_of(tuples, "BGS"), {"GS_TP53", "GS_BRCA1", "GS_EGFR"}
        )

    def test_does_not_contain_subcluster_of(self):
        # BiomarkerCombinationSubclusterOfBinaryGeneSet was removed from the
        # schema.
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0015003" in p for p in preds))

    def test_contains_has_binary_gene_set(self):
        # CellSetHasBinaryGeneSetBinaryGeneSet has its own relation, while the
        # gene edges use selectively_expresses — the two must stay distinct.
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        bgs_edges = [
            t
            for t in tuples
            if len(t) == 3 and "RO_0015013" in str(t[1]) and "/BGS_" in str(t[2])
        ]
        self.assertEqual(len(bgs_edges), 1)

    def test_contains_selectively_expresses_gene_per_binary_gene(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        # CS -[selectively_expresses]-> Gene: predicate RO_0002294, object GS_*.
        # The cell set selectively expresses every binary gene, which always
        # includes the marker genes.
        gene_edges = [
            t for t in tuples
            if len(t) == 3
            and "RO_0002294" in str(t[1])
            and "/GS_" in str(t[2])
        ]
        objects = {str(t[2]).rsplit("/", 1)[-1] for t in gene_edges}
        self.assertEqual(objects, {"GS_TP53", "GS_BRCA1", "GS_EGFR"})

    def test_no_gene_carries_the_binary_gene_set_predicate(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertFalse(
            any(
                len(t) == 3 and "RO_0015013" in str(t[1]) and "/GS_" in str(t[2])
                for t in tuples
            )
        )

    def test_contains_source_quintuple(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        quints = [t for t in tuples if len(t) == 5 and "Source" in str(t[3])]
        self.assertGreater(len(quints), 0)
        sources = {str(t[4]) for t in quints}
        self.assertTrue({"NS-Forest", "CELLxGENE"} & sources)

    def test_skips_small_clusters(self):
        nsf, summary = self._make_data()
        nsf.loc[0, "clusterSize"] = 5  # Below MIN_CLUSTER_SIZE
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertEqual(len(tuples), 0)

    def test_edge_annotations_on_cs_bmc(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        edge_annots = [
            t for t in tuples
            if len(t) == 5 and "RO_0015004" in str(t[1])
        ]
        attrs = [str(t[3]).split("#")[-1] for t in edge_annots]
        self.assertIn("Precision", attrs)
        self.assertIn("TP", attrs)
        self.assertIn("FN", attrs)

    # --- root UBERON term rollup -------------------------------------------

    @staticmethod
    def _anatomical_objects(tuples, predicate):
        """Return the UBERON terms an edge with this predicate connects to."""
        return {
            str(t[2]).rsplit("/", 1)[-1]
            for t in tuples
            if len(t) == 3 and predicate in str(t[1]) and "/UBERON_" in str(t[2])
        }

    def _heart_data(self):
        nsf, summary = self._make_data()
        nsf.loc[0, "clusterName"] = "Pericyte"
        summary.loc[0, "organ"] = "heart_plus_pericardium"
        summary.loc[0, "tissue_ontology_term_id"] = (
            "UBERON:0006566 | UBERON:0006567 | UBERON:0002084"
        )
        return nsf, summary

    def test_cell_set_derives_from_root_term_alone(self):
        nsf, summary = self._heart_data()
        tuples = create_tuples(
            nsf, summary, ["dvid-001"], root_uberon_term="UBERON:0015410"
        )
        # derives_from
        self.assertEqual(
            self._anatomical_objects(tuples, "RO_0001000"), {"UBERON_0015410"}
        )
        # is_about
        self.assertEqual(
            self._anatomical_objects(tuples, "IAO_0000136"), {"UBERON_0015410"}
        )

    def test_sampled_tissue_annotates_the_root_edge(self):
        nsf, summary = self._heart_data()
        tuples = create_tuples(
            nsf, summary, ["dvid-001"], root_uberon_term="UBERON:0015410"
        )
        sampled = {
            str(t[4])
            for t in tuples
            if len(t) == 5 and str(t[3]).endswith("#Sampled_tissue")
        }
        self.assertEqual(
            sampled, {"UBERON:0006566|UBERON:0006567|UBERON:0002084"}
        )

    def test_without_a_root_term_every_tissue_term_connects(self):
        nsf, summary = self._heart_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertEqual(
            self._anatomical_objects(tuples, "RO_0001000"),
            {"UBERON_0006566", "UBERON_0006567", "UBERON_0002084"},
        )

    # --- is_about dataset_version_id resolution ----------------------------

    @staticmethod
    def _is_about_cell_set(tuples):
        """Return {(CSD_term, CS_term)} for the dataset is_about cell set triples.

        A CellSetDataset is also is_about an AnatomicalStructure, which shares
        the is_about predicate, so the object must be a cell set to tell the
        two apart.
        """
        return {
            (str(t[0]).rsplit("/", 1)[-1], str(t[2]).rsplit("/", 1)[-1])
            for t in tuples
            if len(t) == 3 and "/CSD_" in str(t[0]) and "/CS_" in str(t[2])
        }

    def _multi_dataset_data(self):
        nsf = pd.DataFrame({
            "clusterName": ["A", "B"],
            "clusterSize": [50, 60],
            "f_score": [0.9, 0.8],
            "NSForest_markers": ["['TP53']", "['EGFR']"],
            "binary_genes": ["['TP53']", "['EGFR']"],
            "uuid": ["ua", "ub"],
        })
        summary = pd.DataFrame({"tissue_ontology_term_id": ["UBERON:0000966"]})
        return nsf, summary

    def test_single_dvid_is_about_unchanged(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertEqual(
            self._is_about_cell_set(tuples), {("CSD_dvid-001", "CS_abc123")}
        )

    def test_multi_dvid_resolves_one_edge_per_cluster(self):
        nsf, summary = self._multi_dataset_data()
        cluster_dvid_map = {"A": "dv1", "B": "dv2"}
        tuples = create_tuples(
            nsf, summary, ["dv1", "dv2"], None, cluster_dvid_map
        )
        # Each cell set is described by ONLY its mapped dataset — no fan-out.
        self.assertEqual(
            self._is_about_cell_set(tuples),
            {("CSD_dv1", "CS_ua"), ("CSD_dv2", "CS_ub")},
        )

    def test_multi_dvid_unresolved_cluster_raises(self):
        nsf, summary = self._multi_dataset_data()
        cluster_dvid_map = {"A": "dv1"}  # B unmapped
        with self.assertRaises(Exception):
            create_tuples(nsf, summary, ["dv1", "dv2"], None, cluster_dvid_map)

    def test_multi_dvid_unknown_dataset_raises(self):
        nsf, summary = self._multi_dataset_data()
        cluster_dvid_map = {"A": "dv1", "B": "dvX"}  # dvX not a known dataset
        with self.assertRaises(Exception):
            create_tuples(nsf, summary, ["dv1", "dv2"], None, cluster_dvid_map)


class NSForestDatasetNameTestCase(unittest.TestCase):
    """Tests for how NSForestTupleWriter names a cell set dataset."""

    def _make_data(self):
        nsforest = pd.DataFrame({
            "clusterName": ["T Cell"],
            "clusterSize": [1718],
            "f_score": [0.716],
            "NSForest_markers": ["['TP53']"],
            "binary_genes": ["['TP53', 'BRCA1']"],
            "uuid": ["abc123"],
        })
        summary = pd.DataFrame({
            "tissue_ontology_term_id": ["UBERON:0000966"],
            "first_author": ["Sikkema"],
            "year": [2023],
            "journal": ["Nat Med"],
            "dataset_title": ["Lung, 3' v2"],
        })
        return nsforest, summary

    def _annotations(self, tuples, attribute):
        return [
            str(t[2])
            for t in tuples
            if len(t) == 3 and str(t[1]).endswith(f"#{attribute}")
        ]

    def test_dataset_named_by_citation_and_dataset_name(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertEqual(
            self._annotations(tuples, "Name"),
            ["Sikkema (2023) Nat Med - Lung, 3' v2"],
        )

    def test_citation_matches_the_cellxgene_citation(self):
        # The CELLxGENE writer annotates the same vertex, so both must
        # spell the citation the same way.
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        self.assertEqual(self._annotations(tuples, "Citation"), ["Sikkema (2023) Nat Med"])


class CellSetDatasetMetadataTestCase(unittest.TestCase):
    """A cell set carries its dataset's metadata.

    Only the manually mapped clusters used to, which left most cell sets
    without a species, organ, publication, or dataset name
    (Springbok-LLC/nlm-ckn-etl#63).
    """

    def _make_data(self):
        nsforest = pd.DataFrame({
            "clusterName": ["T Cell"],
            "clusterSize": [1718],
            "f_score": [0.716],
            "cluster_header": ["author_cell_type"],
            "NSForest_markers": ["['TP53']"],
            "binary_genes": ["['TP53']"],
            "uuid": ["abc123"],
        })
        summary = pd.DataFrame({
            "tissue_ontology_term_id": ["UBERON:0002048"],
            "organ": ["respiratory system"],
            "doi": ["10.1038/S41591-023-02327-2"],
            "dataset_title": ["Lung, 3' v2"],
            "collection_url": ["https://cellxgene.cziscience.com/collections/c1"],
        })
        return nsforest, summary

    def _annotation(self, tuples, attribute):
        values = [
            str(t[2])
            for t in tuples
            if len(t) == 3
            and str(t[0]).endswith("CS_abc123")
            and str(t[1]).endswith(f"#{attribute}")
        ]
        return values[0] if values else None

    def test_cell_set_carries_dataset_metadata(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(
            nsf, summary, ["dvid-001"], root_uberon_term="UBERON:0002048"
        )
        self.assertEqual(self._annotation(tuples, "species"), "Homo sapiens")
        self.assertEqual(
            self._annotation(tuples, "anatomical_structure"), "UBERON:0002048"
        )
        self.assertEqual(
            self._annotation(tuples, "publication"), "10.1038/s41591-023-02327-2"
        )
        self.assertEqual(self._annotation(tuples, "dataset_name"), "Lung, 3' v2")
        self.assertEqual(
            self._annotation(tuples, "cellxgene_collection"),
            "cellxgene.cziscience.com/collections/c1",
        )
        self.assertEqual(
            self._annotation(tuples, "cellxgene_dataset"),
            "datasets.cellxgene.cziscience.com/dvid-001.h5ad",
        )
        self.assertEqual(
            self._annotation(tuples, "cluster_annotation"), "author_cell_type"
        )

    def test_cell_set_takes_the_dataset_that_describes_it(self):
        # With several datasets in one results file, the cell set must name
        # the dataset its cluster came from, not the first of the list.
        nsf, summary = self._make_data()
        summary = pd.concat([
            summary.assign(dataset_title="Other", doi="10.1000/other"),
            summary,
        ])
        tuples = create_tuples(
            nsf,
            summary,
            ["dvid-001", "dvid-002"],
            cluster_dvid_map={"T Cell": "dvid-002"},
            root_uberon_term="UBERON:0002048",
        )
        self.assertEqual(
            self._annotation(tuples, "cellxgene_dataset"),
            "datasets.cellxgene.cziscience.com/dvid-002.h5ad",
        )

    def test_cell_set_carries_the_dataset_assay_and_donor_age(self):
        # Both slots are dataset-scoped, so a cell set is described the same
        # way whether or not it also has a manual cell type mapping.
        nsf, summary = self._make_data()
        summary = summary.assign(
            assay_ontology_summary=["EFO:0009922: 40"],
            development_stage_summary=["59-year-old stage: 40 | 5-year-old stage: 0"],
        )
        tuples = create_tuples(
            nsf, summary, ["dvid-001"], root_uberon_term="UBERON:0002048"
        )
        self.assertEqual(self._annotation(tuples, "assay"), "EFO:0009922")
        self.assertEqual(
            self._annotation(tuples, "donor_age"), "59-year-old stage: 40"
        )


class MeanBinaryScoreTestCase(unittest.TestCase):
    """The binary gene set carries the mean of its genes' binary scores.

    The scores arrive as a gene × cluster matrix beside the results file,
    so a cluster's mean is taken down its own column over its own binary
    genes.
    """

    def _results(self, **overrides):
        row = {
            "clusterName": ["T Cell"],
            "clusterSize": [1718],
            "f_score": [0.716],
            "NSForest_markers": ["['ENSG00000141510']"],
            "binary_genes": ["['ENSG00000141510', 'ENSG00000012048']"],
            "uuid": ["abc123"],
        }
        row.update({k: [v] for k, v in overrides.items()})
        return pd.DataFrame(row)

    def _scores(self):
        return pd.DataFrame(
            {"T Cell": [0.8, 0.6, 0.1], "B Cell": [0.2, 0.4, 0.9]},
            index=["ENSG00000141510", "ENSG00000012048", "ENSG00000146648"],
        )

    def test_mean_is_taken_over_the_clusters_binary_genes_alone(self):
        # ENSG00000146648 is in the matrix but not this cluster's binary
        # gene set, so including it would drag the mean down to 0.5.
        means = mean_binary_scores(self._results(), self._scores())
        self.assertAlmostEqual(means.iloc[0], 0.7)

    def test_mean_is_taken_down_the_clusters_own_column(self):
        means = mean_binary_scores(
            self._results(clusterName="B Cell"), self._scores()
        )
        self.assertAlmostEqual(means.iloc[0], 0.3)

    def test_versioned_ensembl_ids_still_match(self):
        # resolve_gene_names reads these unversioned, so a version suffix on
        # either side must not silently drop a gene from the mean.
        means = mean_binary_scores(
            self._results(binary_genes="['ENSG00000141510.4', 'ENSG00000012048.2']"),
            self._scores(),
        )
        self.assertAlmostEqual(means.iloc[0], 0.7)

    def test_a_cluster_the_matrix_omits_has_no_mean(self):
        means = mean_binary_scores(self._results(clusterName="NK Cell"), self._scores())
        self.assertTrue(pd.isna(means.iloc[0]))

    def test_a_cluster_whose_genes_the_matrix_omits_has_no_mean(self):
        # Rather than a mean over nothing.
        means = mean_binary_scores(
            self._results(binary_genes="['ENSG00000999999']"), self._scores()
        )
        self.assertTrue(pd.isna(means.iloc[0]))

    def test_binary_gene_set_carries_the_mean(self):
        nsf = self._results()
        nsf["mean_binary_score"] = mean_binary_scores(nsf, self._scores())
        summary = pd.DataFrame({"tissue_ontology_term_id": ["UBERON:0000966"]})
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        values = [
            str(t[2])
            for t in tuples
            if len(t) == 3
            and "/BGS_" in str(t[0])
            and str(t[1]).endswith("#mean_binary_score")
        ]
        # Every edge touching the binary gene set re-annotates it, and
        # write_tuples dedupes; what matters here is the value.
        self.assertEqual(set(values), {"0.7"})

    def test_results_without_binary_scores_leave_the_mean_empty(self):
        # The matrix is sparse in prod, and a results set without one still
        # has to produce its tuples.
        summary = pd.DataFrame({"tissue_ontology_term_id": ["UBERON:0000966"]})
        tuples = create_tuples(self._results(), summary, ["dvid-001"])
        self.assertFalse(
            any(
                len(t) == 3 and str(t[1]).endswith("#mean_binary_score")
                for t in tuples
            )
        )


if __name__ == "__main__":
    unittest.main()
