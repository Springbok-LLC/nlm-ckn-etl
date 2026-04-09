import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from NSForestTupleWriter import create_tuples


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

    def test_contains_gene_part_of_bmc(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("BFO_0000050" in p for p in preds))  # part_of

    def test_contains_subcluster_of(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0015003" in p for p in preds))  # subcluster_of

    def test_contains_expresses(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0002292" in p for p in preds))  # expresses

    def test_contains_source_quintuple(self):
        nsf, summary = self._make_data()
        tuples = create_tuples(nsf, summary, ["dvid-001"])
        quints = [t for t in tuples if len(t) == 5 and "Source" in str(t[3])]
        self.assertGreater(len(quints), 0)
        self.assertTrue(any("NSForest" in str(t[4]) for t in quints))

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


if __name__ == "__main__":
    unittest.main()
