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
            }
        }

    def test_anatomical_structure_part_of(self):
        tuples = create_tuples(self._make_data())
        preds = [str(t[1]) for t in tuples if len(t) == 3 and "#" not in str(t[1])]
        self.assertTrue(any("BFO_0000050" in p for p in preds))

    def test_label_annotations(self):
        tuples = create_tuples(self._make_data())
        labels = [t for t in tuples if len(t) == 3 and "#label" in str(t[1])]
        self.assertGreater(len(labels), 0)


if __name__ == "__main__":
    unittest.main()
