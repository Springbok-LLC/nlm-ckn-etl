import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from CellxGeneTupleWriter import create_tuples


class CellxGeneTupleWriterTestCase(unittest.TestCase):
    """Tests for CellxGeneTupleWriter.create_tuples."""

    def _make_data(self):
        return {
            "dvid-001": {
                "Dataset_name": "Test Dataset",
                "Organism": "Homo sapiens",
                "Tissue": "brain",
                "Disease_status": "normal",
                "Number_of_cells": 10000,
                "Citation": "Smith et al. (2024)",
                "Link_to_publication": "https://doi.org/10.1234/test",
                "Link_to_CELLxGENE_collection": "https://cellxgene.cziscience.com/collections/abc",
                "Link_to_CELLxGENE_dataset": "https://cellxgene.cziscience.com/e/dvid-001.cxg/",
                "Collection_ID": "abc",
                "Collection_version_ID": "cv-001",
                "Dataset_ID": "ds-001",
                "Dataset_version_ID": "dvid-001",
            }
        }

    def test_creates_tuples(self):
        tuples = create_tuples(self._make_data())
        self.assertGreater(len(tuples), 0)

    def test_contains_source_quintuple(self):
        tuples = create_tuples(self._make_data())
        quints = [t for t in tuples if len(t) == 5 and "Source" in str(t[3])]
        self.assertTrue(any("CELLxGENE" in str(t[4]) for t in quints))

    def test_csd_annotations(self):
        tuples = create_tuples(self._make_data())
        csd_annots = [
            t for t in tuples
            if len(t) == 3 and "CSD_" in str(t[0])
        ]
        self.assertGreater(len(csd_annots), 0)


if __name__ == "__main__":
    unittest.main()
