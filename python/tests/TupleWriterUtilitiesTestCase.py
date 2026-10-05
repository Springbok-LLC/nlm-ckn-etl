import json
import sys
import tempfile
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
            "CS_abc123",
        )

    def test_cell_set_without_context(self):
        cs = CellSet(author_cell_term="T-Cell")
        self.assertIsNone(twu.entity_to_term(cs))

    def test_cell_set_dataset(self):
        csd = CellSetDataset(dataset_identifier="5774ef6a-4082")
        self.assertEqual(twu.entity_to_term(csd), "CSD_5774ef6a-4082")

    def test_cell_set_dataset_composite_identifier(self):
        csd = CellSetDataset(dataset_identifier="5774ef6a-4082__kidney")
        self.assertEqual(twu.entity_to_term(csd), "CSD_5774ef6a-4082__kidney")


class CellSetDatasetIdentifierTestCase(unittest.TestCase):
    """Tests for cell_set_dataset_identifier and build_cell_set_dataset keys."""

    def test_organ_makes_a_composite_identifier(self):
        self.assertEqual(
            twu.cell_set_dataset_identifier("5774ef6a-4082", "heart plus pericardium"),
            "5774ef6a-4082__heart_plus_pericardium",
        )

    def test_no_organ_keeps_the_source_identifier(self):
        # Falsy / NaN organ leaves single-organ datasets unchanged.
        self.assertEqual(
            twu.cell_set_dataset_identifier("5774ef6a-4082", None), "5774ef6a-4082"
        )
        self.assertEqual(
            twu.cell_set_dataset_identifier("5774ef6a-4082", ""), "5774ef6a-4082"
        )

    def test_same_source_different_organs_are_distinct(self):
        a = twu.cell_set_dataset_identifier("dvid-1", "kidney")
        b = twu.cell_set_dataset_identifier("dvid-1", "liver")
        self.assertNotEqual(a, b)

    def test_build_sets_composite_id_and_retains_source_version(self):
        import pandas as pd

        summary = pd.DataFrame({"organ": ["kidney"], "dataset_title": ["A kidney set"]})
        csd, _ = twu.build_cell_set_dataset("dvid-1", False, summary_data=summary)
        self.assertEqual(csd.dataset_identifier, "dvid-1__kidney")
        self.assertEqual(csd.version, "dvid-1")

    def test_build_without_organ_keeps_source_id(self):
        import pandas as pd

        summary = pd.DataFrame({"dataset_title": ["No organ column"]})
        csd, _ = twu.build_cell_set_dataset("dvid-1", False, summary_data=summary)
        self.assertEqual(csd.dataset_identifier, "dvid-1")
        self.assertEqual(csd.version, "dvid-1")

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

    def test_publication_keyed_by_doi_not_dataset(self):
        # The dataset a publication was harvested with must not enter its
        # key, or each dataset of a paper gets its own publication vertex.
        pub = Publication(publication_doi="10.1234/test")
        self.assertEqual(
            twu.entity_to_term(pub, {"dataset_version_id": "dvid-001"}),
            "PUB_10.1234-test",
        )

    def test_publication_without_context(self):
        pub = Publication(publication_doi="10.1234/test")
        self.assertEqual(twu.entity_to_term(pub), "PUB_10.1234-test")

    def test_publication_without_doi_falls_back_to_dataset(self):
        pub = Publication(title="Untitled")
        self.assertEqual(
            twu.entity_to_term(pub, {"dataset_version_id": "dvid-001"}),
            "PUB_dvid-001",
        )

    def test_publication_without_doi_or_context(self):
        pub = Publication(title="Untitled")
        self.assertIsNone(twu.entity_to_term(pub))

    def test_normalize_doi(self):
        self.assertEqual(
            twu.normalize_doi("https://doi.org/10.1038/S41591-023-02327-2"),
            "10.1038/s41591-023-02327-2",
        )
        self.assertEqual(twu.normalize_doi("dx.doi.org/10.1234/test"), "10.1234/test")
        self.assertEqual(twu.normalize_doi(" 10.1234/test "), "10.1234/test")
        self.assertIsNone(twu.normalize_doi(None))
        self.assertIsNone(twu.normalize_doi(""))

    def test_doi_to_key(self):
        # The key can hold neither the DOI's slash nor an underscore, which
        # OntologyGraphBuilder treats as the collection separator.
        self.assertEqual(
            twu.doi_to_key("https://doi.org/10.1038/s41591-023-02327-2"),
            "10.1038-s41591-023-02327-2",
        )
        self.assertEqual(twu.doi_to_key("10.1234/a_b c"), "10.1234-a-b-c")
        self.assertIsNone(twu.doi_to_key(None))

    def test_doi_key_is_a_legal_vertex_term(self):
        # OntologyGraphBuilder.createVTuple splits the term on "_" and drops
        # the vertex unless it yields exactly two tokens.
        term = twu.entity_to_term(
            Publication(publication_doi="https://doi.org/10.1126/science.adf6812")
        )
        self.assertEqual(len(term.split("_")), 2)
        self.assertNotIn("/", term)
        self.assertNotIn(":", term)

    def test_drug_with_chembl_context(self):
        drug = Drug(label="Imatinib")
        self.assertEqual(
            twu.entity_to_term(drug, {"chembl_id": "941"}),
            "CHEMBL_941",
        )

    def test_drug_without_context(self):
        drug = Drug(label="Imatinib")
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


class CellSetDatasetIsReferenceDatasetTestCase(unittest.TestCase):
    """Tests for the is_reference_dataset parameter of build_cell_set_dataset.

    ckn-schema v0.0.0-alpha.6 added CellSetDataset.is_reference_dataset.
    There is no default: every call site must decide explicitly, so a
    reference dataset never silently reads as False.
    """

    def test_required_argument_raises_when_omitted(self):
        with self.assertRaises(TypeError):
            twu.build_cell_set_dataset("dvid-1")

    def test_true_is_set_explicitly(self):
        csd, _ = twu.build_cell_set_dataset("dvid-1", True)
        self.assertIs(csd.is_reference_dataset, True)

    def test_false_is_set_explicitly_not_left_unset(self):
        csd, _ = twu.build_cell_set_dataset("dvid-1", False)
        self.assertIs(csd.is_reference_dataset, False)


class GetPredicateUriTestCase(unittest.TestCase):
    """Tests for get_predicate_uri."""

    def test_part_of(self):
        assoc = twu.ASSOCIATION_CLASSES["AnatomicalStructurePartOfAnatomicalStructure"](
            subject=AnatomicalStructure(ontology_purl="UBERON:0000955"),
            predicate="nlm-ckn:part_of",
            object=AnatomicalStructure(ontology_purl="UBERON:0000468"),
        )
        self.assertEqual(
            twu.get_predicate_uri(assoc),
            URIRef("http://purl.obolibrary.org/obo/BFO_0000050"),
        )

    def test_is_about(self):
        assoc = twu.ASSOCIATION_CLASSES["CellSetDatasetIsAboutCellSet"](
            subject=CellSetDataset(dataset_identifier="abc"),
            predicate="nlm-ckn:is_about",
            object=CellSet(author_cell_term="T-Cell"),
        )
        self.assertEqual(
            twu.get_predicate_uri(assoc),
            URIRef("http://purl.obolibrary.org/obo/IAO_0000136"),
        )

    def test_selectively_expresses(self):
        assoc = twu.ASSOCIATION_CLASSES["CellSetSelectivelyExpressesGene"](
            subject=CellSet(author_cell_term="T-Cell"),
            predicate="nlm-ckn:selectively_expresses",
            object=Gene(gene_symbol="TP53"),
        )
        self.assertEqual(
            twu.get_predicate_uri(assoc),
            URIRef("http://purl.obolibrary.org/obo/RO_0002294"),
        )

    def test_has_binary_gene_set_uses_its_own_relation(self):
        # RO_0015013 is a placeholder minted by the schema, not a published RO
        # term (Springbok-LLC/nlm-ckn-schema#12); OntologyElementParser
        # registers its label so the edge is not nameless in the graph.
        assoc = twu.ASSOCIATION_CLASSES["CellSetHasBinaryGeneSetBinaryGeneSet"](
            subject=CellSet(author_cell_term="T-Cell"),
            predicate="nlm-ckn:has_binary_gene_set",
            object=BinaryGeneSet(markers="TP53 BRCA1"),
        )
        self.assertEqual(
            twu.get_predicate_uri(assoc),
            URIRef("http://purl.obolibrary.org/obo/RO_0015013"),
        )


class AssociationToTuplesTestCase(unittest.TestCase):
    """Tests for association_to_tuples."""

    def _make_assoc(self):
        return twu.ASSOCIATION_CLASSES["AnatomicalStructurePartOfAnatomicalStructure"](
            subject=AnatomicalStructure(ontology_purl="UBERON:0000955", label="brain"),
            predicate="nlm-ckn:part_of",
            object=AnatomicalStructure(
                ontology_purl="UBERON:0000468", label="multicellular organism"
            ),
        )

    def test_generates_core_triple(self):
        tuples = twu.association_to_tuples(self._make_assoc())
        triples = [t for t in tuples if len(t) == 3 and "#" not in str(t[1])]
        self.assertEqual(len(triples), 1)
        s, p, o = triples[0]
        self.assertIn("UBERON_0000955", str(s))
        self.assertIn("BFO_0000050", str(p))
        self.assertIn("UBERON_0000468", str(o))

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
        self.assertIn("label", attr_names)

    def test_annotated_terms_parameter_is_noop(self):
        # annotated_terms is retained for backward compatibility but is
        # no longer consulted: both calls must emit annotation triples.
        assoc = self._make_assoc()
        annotated = set()
        tuples1 = twu.association_to_tuples(assoc, annotated_terms=annotated)
        tuples2 = twu.association_to_tuples(assoc, annotated_terms=annotated)

        annotations1 = [t for t in tuples1 if len(t) == 3 and "#" in str(t[1])]
        annotations2 = [t for t in tuples2 if len(t) == 3 and "#" in str(t[1])]

        self.assertGreater(len(annotations1), 0)
        self.assertEqual(len(annotations1), len(annotations2))

    def test_none_subject_returns_empty(self):
        assoc = twu.ASSOCIATION_CLASSES["AnatomicalStructurePartOfAnatomicalStructure"](
            subject=None,
            predicate="nlm-ckn:part_of",
            object=AnatomicalStructure(ontology_purl="UBERON:0000955"),
        )
        self.assertEqual(twu.association_to_tuples(assoc), [])


class EntityToAnnotationTriplesTestCase(unittest.TestCase):
    """Tests for entity_to_annotation_triples."""

    def test_populated_fields(self):
        gene = Gene(
            gene_symbol="TP53", label="tumor protein p53", gene_type="protein-coding"
        )
        triples = twu.entity_to_annotation_triples(gene, "GS_TP53")
        attr_names = [str(t[1]).split("#")[-1] for t in triples]
        self.assertIn("gene_symbol", attr_names)
        self.assertIn("label", attr_names)
        self.assertIn("gene_type", attr_names)

    def test_none_fields_skipped(self):
        gene = Gene(gene_symbol="TP53")
        triples = twu.entity_to_annotation_triples(gene, "GS_TP53")
        # Only gene_symbol is populated; None fields are skipped
        self.assertEqual(len(triples), 1)
        attr_name = str(triples[0][1]).split("#")[-1]
        self.assertEqual(attr_name, "gene_symbol")

    def test_edge_fields_skipped(self):
        bmc = BiomarkerCombination(markers="TP53 BRCA1", f_beta_score=0.85)
        triples = twu.entity_to_annotation_triples(
            bmc, "BMC_abc", edge_fields={"f_beta_score"}
        )
        attr_names = [str(t[1]).split("#")[-1] for t in triples]
        self.assertIn("markers", attr_names)
        self.assertNotIn("f_beta_score", attr_names)

    def test_field_name_mapping(self):
        csd = CellSetDataset(
            dataset_identifier="abc",
            dataset_name="My Dataset",
            disease_status="normal",
        )
        triples = twu.entity_to_annotation_triples(csd, "CSD_abc")
        attr_names = [str(t[1]).split("#")[-1] for t in triples]
        self.assertIn("dataset_name", attr_names)
        self.assertIn("disease_status", attr_names)


class PurlToCurieInContextTestCase(unittest.TestCase):
    """Tests for purl_to_curie used in entity construction."""

    def test_cell_type_from_purl(self):
        from ckn_schema.pydantic.ckn_schema import CellType

        curie = twu.purl_to_curie("http://purl.obolibrary.org/obo/CL_0000235")
        ct = CellType(ontology_purl=curie, label="macrophage")
        self.assertEqual(ct.ontology_purl, "CL:0000235")
        self.assertEqual(ct.label, "macrophage")


class DedupeAnnotationTriplesLastWinsTestCase(unittest.TestCase):
    """Tests for _dedupe_annotation_triples_last_wins."""

    def _annot(self, term, attr, value):
        from LoaderUtilities import PURLBASE, RDFSBASE

        return (
            URIRef(f"{PURLBASE}/{term}"),
            URIRef(f"{RDFSBASE}#{attr}"),
            Literal(str(value)),
        )

    def test_keeps_last_value_per_term_attr(self):
        t1 = self._annot("CHEMBL_941", "label", "Imatinib")
        t2 = self._annot("CHEMBL_941", "label", "IMATINIB")
        out = twu._dedupe_annotation_triples_last_wins([t1, t2])
        self.assertEqual(out, [t2])

    def test_keeps_disjoint_attrs(self):
        t_label = self._annot("CHEMBL_941", "label", "Imatinib")
        t_moa = self._annot("CHEMBL_941", "mechanism_of_action", "BCR-ABL inhibitor")
        out = twu._dedupe_annotation_triples_last_wins([t_label, t_moa])
        self.assertEqual(out, [t_label, t_moa])

    def test_non_annotation_tuples_preserved(self):
        from LoaderUtilities import PURLBASE, RDFSBASE

        core = (
            URIRef(f"{PURLBASE}/CL_0000235"),
            URIRef(f"{PURLBASE}/BFO_0000050"),
            URIRef(f"{PURLBASE}/UBERON_0000955"),
        )
        quint = (
            URIRef(f"{PURLBASE}/CL_0000235"),
            URIRef(f"{PURLBASE}/BFO_0000050"),
            URIRef(f"{PURLBASE}/UBERON_0000955"),
            URIRef(f"{RDFSBASE}#Source"),
            Literal("TestSource"),
        )
        ann = self._annot("CL_0000235", "label", "macrophage")
        out = twu._dedupe_annotation_triples_last_wins([core, ann, quint, core])
        self.assertEqual(out, [core, ann, quint, core])

    def test_empty_input(self):
        self.assertEqual(twu._dedupe_annotation_triples_last_wins([]), [])


class WriteTuplesDedupeTestCase(unittest.TestCase):
    """End-to-end: write_tuples applies last-wins dedup before json.dump."""

    def _make_drug_assoc(self, drug, mutation_rsid="rs12345"):
        return twu.ASSOCIATION_CLASSES["MutationHasPharmacologicalEffectDrug"](
            subject=Mutation(reference_sequence_identifier=mutation_rsid),
            predicate="nlm-ckn:has_pharmacological_effect",
            object=drug,
        )

    def _write_and_read(self, tuples):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.json"
            twu.write_tuples(tuples, out)
            with open(out) as f:
                return json.load(f)["tuples"]

    def _annotation_attrs_for(self, rows, term):
        from LoaderUtilities import PURLBASE, RDFSBASE

        subj = f"{PURLBASE}/{term}"
        prefix = f"{RDFSBASE}#"
        attrs = {}
        for r in rows:
            if len(r) == 3 and r[0] == subj and r[1].startswith(prefix):
                attrs[r[1][len(prefix) :]] = r[2]
        return attrs

    def test_sparse_then_rich_same_term(self):
        ctx = {"chembl_id": "941"}
        sparse = self._make_drug_assoc(Drug(label="Imatinib"))
        rich = self._make_drug_assoc(
            Drug(
                label="Imatinib",
                mechanism_of_action="BCR-ABL inhibitor",
                trade_names="Gleevec",
            )
        )
        tuples = []
        tuples.extend(twu.association_to_tuples(sparse, ctx))
        tuples.extend(twu.association_to_tuples(rich, ctx))

        rows = self._write_and_read(tuples)
        attrs = self._annotation_attrs_for(rows, "CHEMBL_941")
        self.assertEqual(attrs.get("label"), "Imatinib")
        self.assertEqual(attrs.get("mechanism_of_action"), "BCR-ABL inhibitor")
        self.assertEqual(attrs.get("trade_names"), "Gleevec")

    def test_rich_then_sparse_same_term(self):
        ctx = {"chembl_id": "941"}
        rich = self._make_drug_assoc(
            Drug(
                label="Imatinib",
                mechanism_of_action="BCR-ABL inhibitor",
                trade_names="Gleevec",
            )
        )
        sparse = self._make_drug_assoc(Drug(label="IMATINIB"))
        tuples = []
        tuples.extend(twu.association_to_tuples(rich, ctx))
        tuples.extend(twu.association_to_tuples(sparse, ctx))

        rows = self._write_and_read(tuples)
        attrs = self._annotation_attrs_for(rows, "CHEMBL_941")
        # Rich attributes survive; label takes the later (sparse) value.
        self.assertEqual(attrs.get("mechanism_of_action"), "BCR-ABL inhibitor")
        self.assertEqual(attrs.get("trade_names"), "Gleevec")
        self.assertEqual(attrs.get("label"), "IMATINIB")

    def test_conflicting_label_values_last_wins(self):
        ctx = {"chembl_id": "941"}
        a = self._make_drug_assoc(Drug(label="First"))
        b = self._make_drug_assoc(Drug(label="Second"))
        tuples = twu.association_to_tuples(a, ctx) + twu.association_to_tuples(b, ctx)

        rows = self._write_and_read(tuples)
        attrs = self._annotation_attrs_for(rows, "CHEMBL_941")
        self.assertEqual(attrs.get("label"), "Second")

        # And only one label row remains.
        from LoaderUtilities import RDFSBASE

        label_rows = [
            r
            for r in rows
            if len(r) == 3
            and r[1] == f"{RDFSBASE}#label"
            and r[0].endswith("CHEMBL_941")
        ]
        self.assertEqual(len(label_rows), 1)

    def test_unique_term_attribute_pairs_in_output(self):
        ctx = {"chembl_id": "941"}
        a = self._make_drug_assoc(Drug(label="Imatinib", trade_names="Gleevec"))
        b = self._make_drug_assoc(Drug(label="Imatinib", mechanism_of_action="moa"))
        tuples = twu.association_to_tuples(a, ctx) + twu.association_to_tuples(b, ctx)

        rows = self._write_and_read(tuples)
        from LoaderUtilities import RDFSBASE

        seen = set()
        for r in rows:
            if len(r) == 3 and r[1].startswith(f"{RDFSBASE}#"):
                key = (r[0], r[1])
                self.assertNotIn(key, seen)
                seen.add(key)

    def test_core_and_quintuples_not_deduped(self):
        ctx = {"chembl_id": "941"}
        a = self._make_drug_assoc(Drug(label="Imatinib"))
        tuples = twu.association_to_tuples(
            a, ctx, source="S1"
        ) + twu.association_to_tuples(a, ctx, source="S2")

        rows = self._write_and_read(tuples)
        core = [r for r in rows if len(r) == 3 and "#" not in r[1]]
        quints = [r for r in rows if len(r) == 5]
        self.assertEqual(len(core), 2)
        self.assertEqual(len(quints), 2)


class CellSetDatasetSlotSourcesTestCase(unittest.TestCase):
    """The summary is primary and the harvester the fallback.

    The per-organ harvester tables do not cover every dataset, so slots
    sourced from them alone were absent on part of the graph
    (Springbok-LLC/nlm-ckn-etl#63).
    """

    def _summary(self, **overrides):
        import pandas as pd

        row = {
            "organ": "kidney",
            "dataset_title": "A kidney set",
            "doi": "10.1000/ABC",
            "filtered_cell_count": 105445,
            "tissue_ontology_summary": "UBERON:0002113: 105445",
            "assay_ontology_summary": "EFO:0009922: 105445",
            "median_silhouette": 0.39,
            "n_clusters": 75,
        }
        row.update(overrides)
        return pd.DataFrame({k: [v] for k, v in row.items()})

    def _harvester(self, **overrides):
        import pandas as pd

        row = {
            "dataset_version_id": "dvid-1",
            "normal_cell_count": 99999,
            # The harvester spells its rollups with "; ", the summary " | ".
            "tissue_ontology_summary": "UBERON:0002113: 99998; UBERON:0001225: 1",
            "assay_ontology_summary": "EFO:0009922: 99999",
            "donor_id_count": 31,
            "collection_version_id": "cvid-1",
        }
        row.update(overrides)
        return pd.DataFrame({k: [v] for k, v in row.items()}).iloc[0]

    def test_summary_supplies_the_dataset_rollups(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1", False, summary_data=self._summary()
        )
        self.assertEqual(csd.filtered_cell_count, "105445")
        self.assertEqual(csd.tissue_annotation, "UBERON:0002113: 105445")
        self.assertEqual(csd.assay_summary, "EFO:0009922: 105445")
        self.assertEqual(csd.median_of_median_silhouette, 0.39)
        self.assertEqual(csd.cluster_summary, 75)

    def test_summary_wins_over_the_harvester(self):
        # The harvester's normal_cell_count is computed differently than the
        # slot's definition (Springbok-LLC/nlm-ckn-etl#64); the summary's
        # filtered_cell_count is the faithful one.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(),
            harvester_row=self._harvester(),
        )
        self.assertEqual(csd.filtered_cell_count, "105445")
        self.assertEqual(csd.tissue_annotation, "UBERON:0002113: 105445")

    def test_harvester_fills_what_the_summary_leaves_empty(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(
                tissue_ontology_summary=None,
                assay_ontology_summary=None,
            ),
            harvester_row=self._harvester(),
        )
        self.assertEqual(
            csd.tissue_annotation, "UBERON:0002113: 99998 | UBERON:0001225: 1"
        )
        self.assertEqual(csd.assay_summary, "EFO:0009922: 99999")

    def test_filtered_cell_count_never_falls_back_to_normal_cell_count(self):
        # The harvester's normal_cell_count is a different quantity, not the
        # same one computed elsewhere (Springbok-LLC/nlm-ckn-etl#64), so a
        # summary without the count leaves the slot empty rather than
        # carrying a number that does not mean what the slot says.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(filtered_cell_count=None),
            harvester_row=self._harvester(),
        )
        self.assertIsNone(csd.filtered_cell_count)

    def test_donor_count_and_collection_version_come_from_the_harvester(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(),
            harvester_row=self._harvester(),
        )
        self.assertEqual(csd.donor_id_count, 31)
        self.assertEqual(csd.dataset_collection_version, "cvid-1")

    def test_rollups_use_one_separator_whichever_source_they_came_from(self):
        # The summary spells these with " | " and the harvester with "; ",
        # so a consumer that splits them would otherwise have to handle both
        # depending on which source covered the dataset.
        from_summary, _ = twu.build_cell_set_dataset(
            "dvid-1", False, summary_data=self._summary()
        )
        from_harvester, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(
                tissue_ontology_summary=None, assay_ontology_summary=None
            ),
            harvester_row=self._harvester(),
        )
        self.assertEqual(from_summary.tissue_annotation, "UBERON:0002113: 105445")
        self.assertEqual(
            from_harvester.tissue_annotation,
            "UBERON:0002113: 99998 | UBERON:0001225: 1",
        )

    def test_counts_are_whole_numbers_from_a_float_backed_column(self):
        # A count column loads as float64 when any row of its file is blank,
        # and these counts reach the UI as strings.
        import pandas as pd

        summary = pd.DataFrame(
            {
                "organ": ["kidney", "kidney"],
                "filtered_cell_count": [105445.0, None],
                "n_clusters": [75.0, None],
            }
        )
        csd, _ = twu.build_cell_set_dataset("dvid-1", False, summary_data=summary)
        self.assertEqual(csd.filtered_cell_count, "105445")
        self.assertEqual(csd.cluster_summary, 75)

    def test_publication_falls_back_to_the_summary_doi_normalized(self):
        # DOIs are case-insensitive, so the dataset must name the paper the
        # same way its PUB vertex key does.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1", False, summary_data=self._summary()
        )
        self.assertEqual(csd.publication, "10.1000/abc")

    def test_explicit_doi_wins_over_the_summary(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(),
            doi="https://doi.org/10.1000/XYZ",
        )
        self.assertEqual(csd.publication, "10.1000/xyz")

    def test_assay_names_the_terms_its_summary_counts(self):
        # assay and assay_summary come from one rollup: the terms the
        # dataset covers, and how many cells each contributed.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1", False, summary_data=self._summary()
        )
        self.assertEqual(csd.assay, "EFO:0009922")
        self.assertEqual(csd.assay_summary, "EFO:0009922: 105445")

    def test_assay_prefers_the_harvester_term_list_over_its_rollup(self):
        # The harvester names the terms outright, so a dataset the summary
        # does not cover still gets them without going through the rollup.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            harvester_row=self._harvester(
                assay_ontology_term_id="EFO:0009899; EFO:0010010"
            ),
        )
        self.assertEqual(csd.assay, "EFO:0009899 | EFO:0010010")

    def test_assay_falls_back_to_the_harvester_rollup(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1", False, harvester_row=self._harvester()
        )
        self.assertEqual(csd.assay, "EFO:0009922")

    def test_donor_age_comes_from_the_development_stage_summary(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(
                development_stage_summary="59-year-old stage: 40 | 22-year-old stage: 2"
            ),
        )
        self.assertEqual(csd.donor_age, "59-year-old stage: 40 | 22-year-old stage: 2")

    def test_donor_age_drops_the_stages_the_dataset_does_not_cover(self):
        # The summaries enumerate the whole HsapDv vocabulary here, all but
        # a handful at zero, which would put thousands of characters of
        # stages the dataset has no cells for into the slot.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(
                development_stage_summary=(
                    "59-year-old stage: 40 | 1-month-old stage: 0 | "
                    "101-year-old stage: 0 | 22-year-old stage: 2"
                )
            ),
        )
        self.assertEqual(csd.donor_age, "59-year-old stage: 40 | 22-year-old stage: 2")

    def test_donor_age_reads_a_count_published_with_thousands_separators(self):
        # The harvester writes its counts with commas, so a naive int() of
        # the count would treat every four-digit stage as unparsable.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            harvester_row=self._harvester(
                development_stage_summary="30-year-old stage: 34,167; 5-year-old stage: 0"
            ),
        )
        self.assertEqual(csd.donor_age, "30-year-old stage: 34,167")

    def test_donor_age_keeps_a_stage_whose_count_cannot_be_read(self):
        # An unparsable count is not evidence that the dataset has no cells
        # for the stage, so the pair stays rather than being dropped.
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(
                development_stage_summary="59-year-old stage: unknown"
            ),
        )
        self.assertEqual(csd.donor_age, "59-year-old stage: unknown")

    def test_donor_age_is_empty_when_every_stage_is_zero(self):
        csd, _ = twu.build_cell_set_dataset(
            "dvid-1",
            False,
            summary_data=self._summary(
                development_stage_summary="1-month-old stage: 0 | 5-year-old stage: 0"
            ),
        )
        self.assertIsNone(csd.donor_age)


if __name__ == "__main__":
    unittest.main()
