import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from UniProtTupleWriter import create_tuples


class UniProtTupleWriterTestCase(unittest.TestCase):
    """Tests for UniProtTupleWriter.create_tuples."""

    def _make_data(self):
        return {
            "protein_accessions": ["P19022"],
            "P19022": {
                "Protein_name": "Cadherin-2",
                "UniProt_ID": "P19022",
                "Gene_name": "CDH2",
                "Number_of_amino_acids": 906,
                "Function": "Calcium-dependent cell adhesion.",
                "Annotation_score": 5,
                "Organism": "Homo sapiens",
            },
        }

    def test_creates_tuples(self):
        tuples = create_tuples(self._make_data())
        self.assertGreater(len(tuples), 0)

    def test_all_annotations(self):
        tuples = create_tuples(self._make_data())
        self.assertTrue(all(len(t) == 3 for t in tuples))
        attrs = [str(t[1]).split("#")[-1] for t in tuples]
        self.assertIn("Label", attrs)
        self.assertIn("Function", attrs)
        self.assertIn("Species", attrs)
        self.assertIn("Gene_symbol", attrs)
        self.assertIn("Annotation_score", attrs)
        self.assertIn("Number_of_amino_acids", attrs)

    def test_skips_none_function(self):
        data = self._make_data()
        data["P19022"]["Function"] = None
        tuples = create_tuples(data)
        attrs = [str(t[1]).split("#")[-1] for t in tuples]
        self.assertNotIn("Function", attrs)

    def test_uniprot_id_term_encoded(self):
        tuples = create_tuples(self._make_data())
        attrs = [str(t[1]).split("#")[-1] for t in tuples]
        self.assertNotIn("Uniprot_id", attrs)


if __name__ == "__main__":
    unittest.main()
