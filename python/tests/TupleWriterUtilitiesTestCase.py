import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rdflib.term import Literal, URIRef

from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    BinaryGeneSet,
    BiomarkerCombination,
    CellSet,
    CellSetDataset,
    CellType,
    ClinicalTrial,
    Disease,
    Drug,
    Gene,
    Mutation,
    Protein,
    Publication,
    VariantConsequence,
)

import TupleWriterUtilities as twu


class PurlToCurieTestCase(unittest.TestCase):
    """Tests for purl_to_curie."""

    def test_http_purl(self):
        self.assertEqual(
            twu.purl_to_curie("http://purl.obolibrary.org/obo/CL_0000235"),
            "CL:0000235",
        )

    def test_https_purl(self):
        self.assertEqual(
            twu.purl_to_curie("https://purl.obolibrary.org/obo/CL_4030027"),
            "CL:4030027",
        )

    def test_already_curie(self):
        self.assertEqual(twu.purl_to_curie("UBERON:0000955"), "UBERON:0000955")

    def test_mondo(self):
        self.assertEqual(
            twu.purl_to_curie("http://purl.obolibrary.org/obo/MONDO_0009061"),
            "MONDO:0009061",
        )


class ParseStringListTestCase(unittest.TestCase):
    """Tests for parse_string_list."""

    def test_valid_list(self):
        self.assertEqual(
            twu.parse_string_list("['SLC12A7', 'OTOGL']"),
            ["SLC12A7", "OTOGL"],
        )

    def test_empty_list(self):
        self.assertEqual(twu.parse_string_list("[]"), [])

    def test_invalid_string(self):
        self.assertEqual(twu.parse_string_list("not a list"), [])

    def test_single_element(self):
        self.assertEqual(twu.parse_string_list("['TP53']"), ["TP53"])


class CurieToTermTestCase(unittest.TestCase):
    """Tests for curie_to_term."""

    def test_cl_curie(self):
        self.assertEqual(twu.curie_to_term("CL:0000235"), "CL_0000235")

    def test_uberon_curie(self):
        self.assertEqual(twu.curie_to_term("UBERON:0000955"), "UBERON_0000955")

    def test_already_underscore(self):
        self.assertEqual(twu.curie_to_term("CL_0000235"), "CL_0000235")


class RemoveProtocolsTestCase(unittest.TestCase):
    """Tests for remove_protocols."""

    def test_http(self):
        self.assertEqual(twu.remove_protocols("http://example.com"), "example.com")

    def test_https(self):
        self.assertEqual(twu.remove_protocols("https://example.com"), "example.com")

    def test_non_string(self):
        self.assertEqual(twu.remove_protocols(42), 42)

    def test_none(self):
        self.assertIsNone(twu.remove_protocols(None))


class EntityToTermTestCase(unittest.TestCase):
    """Tests for entity_to_term."""

    def test_cell_type(self):
        ct = CellType(ontology_purl="CL:0000235")
        self.assertEqual(twu.entity_to_term(ct), "CL_0000235")

    def test_anatomical_structure(self):
        anat = AnatomicalStructure(ontology_purl="UBERON:0000955")
        self.assertEqual(twu.entity_to_term(anat), "UBERON_0000955")

    def test_gene(self):
        gene = Gene(gene_symbol="TP53")
        self.assertEqual(twu.entity_to_term(gene), "GS_TP53")

    def test_protein_with_uniprot_id(self):
        protein = Protein(gene_symbol="CFTR", uniprot_id="P13569")
        self.assertEqual(twu.entity_to_term(protein), "PR_P13569")

    def test_protein_without_uniprot_id(self):
        protein = Protein(gene_symbol="CFTR")
        self.assertIsNone(twu.entity_to_term(protein))

    def test_cell_set_with_context(self):
        cs = CellSet(author_cell_term="T-Cell")
        self.assertEqual(
            twu.entity_to_term(cs, {"uuid": "abc123"}),
            "CS_T-Cell-abc123",
        )

    def test_cell_set_without_context(self):
        cs = CellSet(author_cell_term="T-Cell")
        self.assertIsNone(twu.entity_to_term(cs))

    def test_cell_set_dataset(self):
        csd = CellSetDataset(dataset_identifier="5774ef6a-4082")
        self.assertEqual(twu.entity_to_term(csd), "CSD_5774ef6a-4082")

    def test_biomarker_combination_with_context(self):
        bmc = BiomarkerCombination(markers="TP53 BRCA1")
        self.assertEqual(
            twu.entity_to_term(bmc, {"uuid": "xyz789"}),
            "BMC_xyz789",
        )

    def test_binary_gene_set_with_context(self):
        bgs = BinaryGeneSet(markers="TP53 BRCA1 EGFR")
        self.assertEqual(
            twu.entity_to_term(bgs, {"uuid": "xyz789"}),
            "BGS_xyz789",
        )

    def test_publication(self):
        pub = Publication(pmid="12345678")
        self.assertEqual(twu.entity_to_term(pub), "PUB_12345678")

    def test_drug_with_chembl_context(self):
        drug = Drug(drug_name="Imatinib")
        self.assertEqual(
            twu.entity_to_term(drug, {"chembl_id": "941"}),
            "CHEMBL_941",
        )

    def test_drug_without_context(self):
        drug = Drug(drug_name="Imatinib")
        self.assertEqual(twu.entity_to_term(drug), "DRUG_Imatinib")

    def test_clinical_trial(self):
        ct = ClinicalTrial(study_id="NCT00494511")
        self.assertEqual(twu.entity_to_term(ct), "NCT_00494511")

    def test_mutation(self):
        mut = Mutation(reference_sequence_identifier="rs2070673")
        self.assertEqual(twu.entity_to_term(mut), "RS_2070673")

    def test_disease_fallback(self):
        disease = Disease(ontology_purl="MONDO:0009061")
        self.assertEqual(twu.entity_to_term(disease), "MONDO_0009061")

    def test_variant_consequence_fallback(self):
        vc = VariantConsequence(ontology_purl="SO:0001819")
        self.assertEqual(twu.entity_to_term(vc), "SO_0001819")


class GetPredicateUriTestCase(unittest.TestCase):
    """Tests for get_predicate_uri."""

    def test_part_of(self):
        assoc = twu.ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
            subject=CellType(ontology_purl="CL:0000235"),
            predicate="part_of",
            object=AnatomicalStructure(ontology_purl="UBERON:0000955"),
        )
        self.assertEqual(
            twu.get_predicate_uri(assoc),
            URIRef("http://purl.obolibrary.org/obo/BFO_0000050"),
        )

    def test_selectively_expresses(self):
        assoc = twu.ASSOCIATION_CLASSES["CellTypeExpressesGene"](
            subject=CellType(ontology_purl="CL:0000235"),
            predicate="selectively_expresses",
            object=Gene(gene_symbol="TP53"),
        )
        self.assertEqual(
            twu.get_predicate_uri(assoc),
            URIRef("http://purl.obolibrary.org/obo/RO_0002294"),
        )

    def test_source(self):
        assoc = twu.ASSOCIATION_CLASSES["CellSetHasSourceCellSetDataset"](
            subject=CellSet(author_cell_term="T-Cell"),
            predicate="source",
            object=CellSetDataset(dataset_identifier="abc"),
        )
        uri = twu.get_predicate_uri(assoc)
        self.assertIn("dc/elements/1.1/source", str(uri))


class AssociationToTuplesTestCase(unittest.TestCase):
    """Tests for association_to_tuples."""

    def _make_assoc(self):
        return twu.ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
            subject=CellType(ontology_purl="CL:0000235", label="macrophage"),
            predicate="part_of",
            object=AnatomicalStructure(ontology_purl="UBERON:0000955", label="brain"),
        )

    def test_generates_core_triple(self):
        tuples = twu.association_to_tuples(self._make_assoc())
        triples = [t for t in tuples if len(t) == 3 and "#" not in str(t[1])]
        self.assertEqual(len(triples), 1)
        s, p, o = triples[0]
        self.assertIn("CL_0000235", str(s))
        self.assertIn("BFO_0000050", str(p))
        self.assertIn("UBERON_0000955", str(o))

    def test_generates_source_quintuple(self):
        tuples = twu.association_to_tuples(self._make_assoc(), source="TestSource")
        quints = [t for t in tuples if len(t) == 5]
        self.assertTrue(any("Source" in str(t[3]) for t in quints))
        self.assertTrue(any("TestSource" in str(t[4]) for t in quints))

    def test_no_source_without_param(self):
        tuples = twu.association_to_tuples(self._make_assoc())
        quints = [t for t in tuples if len(t) == 5]
        self.assertEqual(len(quints), 0)

    def test_generates_vertex_annotations(self):
        tuples = twu.association_to_tuples(self._make_assoc())
        annotations = [t for t in tuples if len(t) == 3 and "#" in str(t[1])]
        attr_names = [str(t[1]).split("#")[-1] for t in annotations]
        self.assertIn("Label", attr_names)

    def test_annotated_terms_deduplication(self):
        assoc = self._make_assoc()
        annotated = set()
        tuples1 = twu.association_to_tuples(assoc, annotated_terms=annotated)
        tuples2 = twu.association_to_tuples(assoc, annotated_terms=annotated)

        annotations1 = [t for t in tuples1 if len(t) == 3 and "#" in str(t[1])]
        annotations2 = [t for t in tuples2 if len(t) == 3 and "#" in str(t[1])]

        self.assertGreater(len(annotations1), 0)
        self.assertEqual(len(annotations2), 0)

    def test_annotated_terms_tracks_terms(self):
        annotated = set()
        twu.association_to_tuples(self._make_assoc(), annotated_terms=annotated)
        self.assertIn("CL_0000235", annotated)
        self.assertIn("UBERON_0000955", annotated)

    def test_none_subject_returns_empty(self):
        assoc = twu.ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
            subject=None,
            predicate="part_of",
            object=AnatomicalStructure(ontology_purl="UBERON:0000955"),
        )
        self.assertEqual(twu.association_to_tuples(assoc), [])


class EntityToAnnotationTriplesTestCase(unittest.TestCase):
    """Tests for entity_to_annotation_triples."""

    def test_populated_fields(self):
        gene = Gene(gene_symbol="TP53", label="tumor protein p53", gene_type="protein-coding")
        triples = twu.entity_to_annotation_triples(gene, "GS_TP53")
        attr_names = [str(t[1]).split("#")[-1] for t in triples]
        self.assertIn("Label", attr_names)
        self.assertIn("Gene_type", attr_names)
        # gene_symbol is term-encoded, should be skipped
        self.assertNotIn("Gene_symbol", attr_names)

    def test_none_fields_skipped(self):
        gene = Gene(gene_symbol="TP53")
        triples = twu.entity_to_annotation_triples(gene, "GS_TP53")
        # Only gene_symbol field is populated but it's term-encoded
        self.assertEqual(len(triples), 0)

    def test_edge_fields_skipped(self):
        bmc = BiomarkerCombination(markers="TP53 BRCA1", f_beta_score=0.85)
        triples = twu.entity_to_annotation_triples(
            bmc, "BMC_abc", edge_fields={"f_beta_score"}
        )
        attr_names = [str(t[1]).split("#")[-1] for t in triples]
        self.assertIn("Markers", attr_names)
        self.assertNotIn("F_beta_confidence_score", attr_names)

    def test_field_name_mapping(self):
        csd = CellSetDataset(
            dataset_identifier="abc",
            dataset_name="My Dataset",
            disease_status="normal",
        )
        triples = twu.entity_to_annotation_triples(csd, "CSD_abc")
        attr_names = [str(t[1]).split("#")[-1] for t in triples]
        self.assertIn("Dataset_name", attr_names)
        self.assertIn("Disease_status", attr_names)


class PurlToCurieInContextTestCase(unittest.TestCase):
    """Tests for purl_to_curie used in entity construction."""

    def test_cell_type_from_purl(self):
        from ckn_schema.pydantic.ckn_schema import CellType
        curie = twu.purl_to_curie("http://purl.obolibrary.org/obo/CL_0000235")
        ct = CellType(ontology_purl=curie, label="macrophage")
        self.assertEqual(ct.ontology_purl, "CL:0000235")
        self.assertEqual(ct.label, "macrophage")


if __name__ == "__main__":
    unittest.main()
