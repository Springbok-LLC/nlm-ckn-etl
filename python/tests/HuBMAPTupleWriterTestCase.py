import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from HuBMAPTupleWriter import create_tuples


class HuBMAPTupleWriterTestCase(unittest.TestCase):
    """Tests for HuBMAPTupleWriter.create_tuples."""

    def _make_data(self):
        return {
            "data": {
                "anatomical_structures": [
                    {
                        "id": "UBERON:0000955",
                        "ccf_pref_label": "brain",
                        "ccf_part_of": ["UBERON:0000468"],
                    }
                ],
                "cell_types": [
                    {
                        "id": "CL:0000235",
                        "ccf_pref_label": "macrophage",
                        "ccf_located_in": ["UBERON:0000955"],
                    }
                ],
            }
        }

    def test_anatomical_structure_part_of(self):
        tuples = create_tuples(self._make_data(), set())
        preds = [str(t[1]) for t in tuples if len(t) == 3 and "#" not in str(t[1])]
        self.assertTrue(any("BFO_0000050" in p for p in preds))

    def test_cell_type_part_of_requires_cl_terms(self):
        tuples = create_tuples(self._make_data(), set())
        subjects = [str(t[0]) for t in tuples if len(t) == 3 and "#" not in str(t[1])]
        self.assertFalse(any("CL_0000235" in s for s in subjects))

    def test_cell_type_part_of_with_cl_terms(self):
        tuples = create_tuples(self._make_data(), {"CL_0000235"})
        subjects = [str(t[0]) for t in tuples if len(t) == 3 and "#" not in str(t[1])]
        self.assertTrue(any("CL_0000235" in s for s in subjects))

    def test_label_annotations(self):
        tuples = create_tuples(self._make_data(), {"CL_0000235"})
        labels = [t for t in tuples if len(t) == 3 and "Label" in str(t[1])]
        self.assertGreater(len(labels), 0)


if __name__ == "__main__":
    unittest.main()
