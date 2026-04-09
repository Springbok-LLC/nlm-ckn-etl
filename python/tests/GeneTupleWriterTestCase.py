import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from GeneTupleWriter import create_tuples


class GeneTupleWriterTestCase(unittest.TestCase):
    """Tests for GeneTupleWriter.create_tuples."""

    def _make_data(self):
        return {
            "gene_entrez_ids": ["1000"],
            "1000": {
                "Gene_ID": "1000",
                "Official_symbol": "CDH2",
                "Official_full_name": "cadherin 2",
                "Gene_type": "protein-coding",
                "Organism": "Homo sapiens",
                "UniProt_name": "P19022",
                "Link_to_UniProt_ID": "https://www.uniprot.org/uniprot/P19022",
                "RefSeq_gene_ID": "GCF_000001405.40",
                "Also_known_as": ["NCAD", "CDw325"],
                "Summary": "A calcium-dependent cell adhesion protein.",
                "mRNA_(NM)_and_protein_(NP)_sequences": "NM_001792 -> NP_001783",
            },
        }

    def test_creates_tuples(self):
        tuples = create_tuples(self._make_data())
        self.assertGreater(len(tuples), 0)

    def test_contains_produces_relation(self):
        tuples = create_tuples(self._make_data())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0003000" in p for p in preds))

    def test_gene_annotations(self):
        tuples = create_tuples(self._make_data())
        gene_annots = [
            t for t in tuples
            if len(t) == 3 and "GS_" in str(t[0]) and "#" in str(t[1])
        ]
        attrs = [str(t[1]).split("#")[-1] for t in gene_annots]
        self.assertIn("Label", attrs)
        self.assertIn("Gene_type", attrs)
        self.assertIn("Species", attrs)

    def test_exact_synonym_joined(self):
        tuples = create_tuples(self._make_data())
        synonym_tuples = [
            t for t in tuples
            if len(t) == 3 and "Exact_synonym" in str(t[1])
        ]
        self.assertEqual(len(synonym_tuples), 1)
        self.assertIn("NCAD", str(synonym_tuples[0][2]))
        self.assertNotIn("[", str(synonym_tuples[0][2]))


if __name__ == "__main__":
    unittest.main()
