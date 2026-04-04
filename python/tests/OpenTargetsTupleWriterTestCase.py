import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from OpenTargetsTupleWriter import create_tuples


class OpenTargetsTupleWriterTestCase(unittest.TestCase):
    """Tests for OpenTargetsTupleWriter.create_tuples."""

    def _make_data(self):
        ot = {
            "gene_ensembl_ids": ["ENSG00000001626"],
            "ENSG00000001626": {
                "diseases": [
                    {
                        "disease": {
                            "id": "MONDO_0009061",
                            "name": "cystic fibrosis",
                            "description": "A genetic disorder.",
                        },
                        "score": 0.8,
                    }
                ],
                "drugs": [],
                "interactions": [],
                "pharmacogenetics": [],
                "tractability": [],
                "expression": [],
                "depmap": [],
            },
        }
        gene = {
            "gene_entrez_ids": ["1080"],
            "1080": {
                "UniProt_name": "P13569",
                "Link_to_UniProt_ID": "https://www.uniprot.org/uniprot/P13569",
            },
        }
        return ot, gene

    def test_creates_disease_tuples(self):
        ot, gene = self._make_data()
        tuples = create_tuples(ot, gene)
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0004010" in p for p in preds))

    def test_disease_score_edge_annotation(self):
        ot, gene = self._make_data()
        tuples = create_tuples(ot, gene)
        score_quints = [
            t for t in tuples
            if len(t) == 5 and "Score" in str(t[3])
        ]
        self.assertGreater(len(score_quints), 0)

    def test_skips_low_score_diseases(self):
        ot, gene = self._make_data()
        ot["ENSG00000001626"]["diseases"][0]["score"] = 0.2
        tuples = create_tuples(ot, gene)
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0004010" in p for p in preds))


if __name__ == "__main__":
    unittest.main()
