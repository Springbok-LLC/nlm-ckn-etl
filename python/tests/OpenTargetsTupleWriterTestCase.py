import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from OpenTargetsTupleWriter import create_tuples


class OpenTargetsTupleWriterTestCase(unittest.TestCase):
    """Tests for OpenTargetsTupleWriter.create_tuples."""

    # ----- Builders -----

    def _make_gene_results(self):
        """Return minimal NCBI Gene results for CFTR (Entrez 1080)."""
        return {
            "gene_entrez_ids": ["1080"],
            "1080": {
                "UniProt_name": "P13569",
                "Link_to_UniProt_ID": "https://www.uniprot.org/uniprot/P13569",
            },
        }

    def _make_ot_base(self):
        """Return Open Targets data with empty resource lists."""
        return {
            "gene_ensembl_ids": ["ENSG00000001626"],
            "ENSG00000001626": {
                "diseases": [],
                "drugs": [],
                "interactions": [],
                "pharmacogenetics": [],
                "tractability": [],
                "expression": [],
                "depmap": [],
            },
        }

    def _make_disease(self, **overrides):
        """Return a minimal disease-association entry."""
        entry = {
            "disease": {
                "id": "MONDO_0009061",
                "name": "cystic fibrosis",
                "description": "A genetic disorder.",
            },
            "score": 0.8,
        }
        entry.update(overrides)
        return entry

    def _make_drug(self, **overrides):
        """Return a minimal drug entry with optional overrides."""
        drug = {
            "drug": {
                "id": "CHEMBL123",
                "name": "TestDrug",
                "description": "A test drug",
                "drugType": "SmallMolecule",
                "maximumClinicalStage": "APPROVAL",
                "drugWarnings": [],
                "synonyms": [],
                "tradeNames": [],
                "mechanismsOfAction": {"rows": []},
                "indications": None,
            },
        }
        drug["drug"].update(overrides)
        return drug

    def _make_indication(self, **overrides):
        """Return a minimal indication row for a drug."""
        entry = {
            "disease": {
                "id": "MONDO_0009061",
                "name": "cystic fibrosis",
                "description": "A genetic disorder.",
            },
            "maxClinicalStage": "APPROVAL",
            "clinicalReports": [],
        }
        entry.update(overrides)
        return entry

    def _make_pharmacogenetics(self, **overrides):
        """Return a minimal pharmacogenetics entry."""
        entry = {
            "variantRsId": "rs123456",
            "variantFunctionalConsequenceId": "SO_0001583",
            "variantFunctionalConsequence": "missense_variant",
            "genotypeId": "gt1",
            "genotype": "AA",
            "phenotypeText": "altered response",
            "genotypeAnnotationText": "annotation",
            "evidenceLevel": "1A",
            "datasourceId": "pharmgkb",
            "literature": "PMID:12345",
            "drugs": [],
        }
        entry.update(overrides)
        return entry

    # ----- Disease tests -----

    def test_creates_disease_tuples(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["diseases"] = [self._make_disease()]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0004010" in p for p in preds))

    def test_disease_score_edge_annotation(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["diseases"] = [self._make_disease()]
        tuples = create_tuples(ot, self._make_gene_results())
        score_quints = [
            t for t in tuples if len(t) == 5 and "Score" in str(t[3])
        ]
        self.assertGreater(len(score_quints), 0)

    def test_skips_low_score_diseases(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["diseases"] = [self._make_disease(score=0.2)]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0004010" in p for p in preds))

    # ----- Drug tests -----

    def test_creates_drug_protein_interaction_tuples(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["drugs"] = [self._make_drug()]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        # molecularly_interacts_with = RO_0002436
        mol_preds = [p for p in preds if "RO_0002436" in p]
        self.assertGreaterEqual(len(mol_preds), 1)

    def test_skips_drug_wrong_phase(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["drugs"] = [
            self._make_drug(maximumClinicalStage="PHASE_1")
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0002436" in p for p in preds))

    def test_skips_withdrawn_drug(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["drugs"] = [
            self._make_drug(drugWarnings=[{"warningType": "Withdrawn"}])
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0002436" in p for p in preds))

    def test_drug_treats_disease(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["drugs"] = [
            self._make_drug(
                indications={"rows": [self._make_indication()]}
            )
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        # is_substance_that_treats = RO_0002606
        self.assertTrue(any("RO_0002606" in p for p in preds))

    def test_drug_evaluated_in_clinical_trial(self):
        ot = self._make_ot_base()
        indication = self._make_indication(
            clinicalReports=[{"id": "NCT00000001"}]
        )
        ot["ENSG00000001626"]["drugs"] = [
            self._make_drug(indications={"rows": [indication]})
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        # evaluated_in = RO_0020325
        self.assertTrue(any("RO_0020325" in p for p in preds))

    def test_skips_non_nct_clinical_trial(self):
        ot = self._make_ot_base()
        indication = self._make_indication(
            clinicalReports=[{"id": "EUCTR2020-001234"}]
        )
        ot["ENSG00000001626"]["drugs"] = [
            self._make_drug(indications={"rows": [indication]})
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0020325" in p for p in preds))

    def test_drug_interacts_with_protein(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["drugs"] = [self._make_drug()]
        tuples = create_tuples(ot, self._make_gene_results())
        # Drug molecularly_interacts_with Protein produces a PR_ term
        subjects = [str(t[0]) for t in tuples if len(t) == 3]
        objects = [str(t[2]) for t in tuples if len(t) == 3]
        all_terms = subjects + objects
        self.assertTrue(any("PR_" in t for t in all_terms))

    # ----- Pharmacogenetics tests -----

    def test_creates_mutation_tuples(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["pharmacogenetics"] = [
            self._make_pharmacogenetics()
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        # has_quality = RO_0000086
        self.assertTrue(any("RO_0000086" in p for p in preds))

    def test_skips_pharmacogenetics_missing_rs_id(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["pharmacogenetics"] = [
            self._make_pharmacogenetics(variantRsId=None)
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0000086" in p for p in preds))

    def test_mutation_has_pharmacological_effect_drug(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["pharmacogenetics"] = [
            self._make_pharmacogenetics(
                variantFunctionalConsequenceId=None,
                drugs=[{"drugId": "CHEMBL456", "drugFromSource": "PharmDrug"}],
            )
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        # has_pharmacological_effect = RO_0002027
        self.assertTrue(any("RO_0002027" in p for p in preds))

    def test_skips_pharmacogenetics_missing_drug_id(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["pharmacogenetics"] = [
            self._make_pharmacogenetics(
                variantFunctionalConsequenceId=None,
                drugs=[{"drugId": None, "drugFromSource": "Unknown"}],
            )
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertFalse(any("RO_0002027" in p for p in preds))

    def test_variant_consequence_manual_tuples(self):
        ot = self._make_ot_base()
        ot["ENSG00000001626"]["pharmacogenetics"] = [
            self._make_pharmacogenetics()
        ]
        tuples = create_tuples(ot, self._make_gene_results())
        # RO_0002331 (involved_in) manual triple for Mutation→VariantConsequence
        preds = [str(t[1]) for t in tuples if len(t) == 3]
        self.assertTrue(any("RO_0002331" in p for p in preds))
        # Should also have VariantConsequence label annotation
        vc_labels = [
            t for t in tuples
            if len(t) == 3 and "Variant_consequence_label" in str(t[1])
        ]
        self.assertGreater(len(vc_labels), 0)


if __name__ == "__main__":
    unittest.main()
