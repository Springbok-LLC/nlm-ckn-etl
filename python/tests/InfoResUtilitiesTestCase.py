from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import InfoResUtilities as iru


class SourceToInfoResTestCase(unittest.TestCase):
    """Pure unit tests for InfoResUtilities.source_to_infores."""

    def test_every_current_source(self):
        """Pins the CURIE for each Source value in the v1.7.0-rc.2 golden dump."""
        expected = {
            "CL": "infores:cl",
            "GO": "infores:go",
            "HP": "infores:hpo",
            "HSAPDV": "infores:hsapdv",
            "MONDO": "infores:mondo",
            "PATO": "infores:pato",
            "PR": "infores:pr",
            "UBERON": "infores:uberon",
            "Open Targets": "infores:open-targets",
            "Open Targets and Gene": "infores:open-targets",
            "UniProt": "infores:uniprot",
            "NS-Forest": "infores:nlm-ckn",
            "Manual Mapping": "infores:nlm-ckn",
            "CELLxGENE": None,
        }
        for source, curie in expected.items():
            with self.subTest(source=source):
                self.assertEqual(iru.source_to_infores(source), curie)

    def test_ncbitaxon_from_ontology_builder(self):
        """Covers the taxslim name OntologyGraphBuilder can emit."""
        self.assertEqual(iru.source_to_infores("NCBITAXON"), "infores:ncbi-taxon")

    def test_ignores_case_and_whitespace(self):
        """Java upper-cases ontology names; the tuple writers use mixed case."""
        self.assertEqual(iru.source_to_infores(" uniprot "), "infores:uniprot")
        self.assertEqual(iru.source_to_infores("OPEN TARGETS"), "infores:open-targets")
        self.assertEqual(iru.source_to_infores("ns-forest"), "infores:nlm-ckn")

    def test_unknown_and_empty_return_none(self):
        """Leaves the fallback to the caller."""
        for source in ("CHEBI", "", None):
            with self.subTest(source=source):
                self.assertIsNone(iru.source_to_infores(source))

    def test_nlm_ckn_constant(self):
        self.assertEqual(iru.NLM_CKN_INFORES, "infores:nlm-ckn")


if __name__ == "__main__":
    unittest.main()
