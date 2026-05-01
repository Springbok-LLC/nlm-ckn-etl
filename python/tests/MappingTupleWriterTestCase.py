import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from MappingTupleWriter import create_tuples


class MappingTupleWriterTestCase(unittest.TestCase):
    """Tests for MappingTupleWriter.create_tuples."""

    def _make_data(self):
        return pd.DataFrame({
            "PMID": [38014002],
            "PMCID": ["PMC10680922"],
            "DOI": ["doi.org/10.1101/2023.11.07.566105"],
            "mapping_method": ["manual"],
            "uberon_entity_term": ["retina"],
            "uberon_entity_id": ["http://purl.obolibrary.org/obo/UBERON_0000966"],
            "author_cell_set": ["T-Cell"],
            "match": ["skos:broadMatch"],
            "cell_ontology_term": ["T cell"],
            "cell_ontology_id": ["http://purl.obolibrary.org/obo/CL_0000084"],
            "clusterName": ["T-Cell"],
            "clusterSize": [1718],
            "NSForest_markers": ["['TP53', 'BRCA1']"],
            "binary_genes": ["['TP53', 'BRCA1', 'EGFR']"],
            "uuid": ["abc123"],
            "collection_id": ["coll-001"],
            "collection_version_id": ["cv-001"],
            "dataset_version_id": ["dv-001"],
        })

    def test_creates_tuples(self):
        tuples = create_tuples(self._make_data(), ["dv-001"])
        self.assertGreater(len(tuples), 0)

    def test_contains_part_of(self):
        tuples = create_tuples(self._make_data(), ["dv-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("BFO_0000050" in p for p in preds))

    def test_contains_composed_primarily_of(self):
        tuples = create_tuples(self._make_data(), ["dv-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0002473" in p for p in preds))

    def test_contains_has_exemplar_data(self):
        tuples = create_tuples(self._make_data(), ["dv-001"])
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0015001" in p for p in preds))

    def test_match_edge_annotation(self):
        tuples = create_tuples(self._make_data(), ["dv-001"])
        edge_annots = [
            t for t in tuples
            if len(t) == 5 and "Match" in str(t[3])
        ]
        self.assertGreater(len(edge_annots), 0)
        self.assertIn("skos:broadMatch", str(edge_annots[0][4]))

    def test_skips_non_cl_ids(self):
        data = self._make_data()
        data.loc[0, "cell_ontology_id"] = "http://purl.obolibrary.org/obo/UBERON_0004225"
        tuples = create_tuples(data, ["dv-001"])
        self.assertEqual(len(tuples), 0)

    def test_skips_small_clusters(self):
        data = self._make_data()
        data.loc[0, "clusterSize"] = 5
        tuples = create_tuples(data, ["dv-001"])
        self.assertEqual(len(tuples), 0)


if __name__ == "__main__":
    unittest.main()
