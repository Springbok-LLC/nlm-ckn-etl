import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from DataTransformer import (
    CellxGeneTransformer,
    GeneTransformer,
    OpenTargetsTransformer,
    UniProtTransformer,
)

FIXTURE_DIR = Path(__file__).parent / "data" / "external"


class CellxGeneTransformerTestCase(unittest.TestCase):
    """Tests for CellxGeneTransformer using fixture data."""

    def setUp(self):
        with open(FIXTURE_DIR / "cellxgene_raw_fixture.json") as f:
            self.raw = json.load(f)
        with open(FIXTURE_DIR / "cellxgene_expected_fixture.json") as f:
            self.expected = json.load(f)

    def test_transform_produces_expected_output(self):
        """Transformer output matches old fetcher output."""
        transformer = CellxGeneTransformer()
        result = transformer.transform(self.raw)

        for key in self.expected:
            self.assertIn(key, result)
            self.assertEqual(result[key], self.expected[key])

    def test_transform_extracts_citation(self):
        """Citation field is present and well-formed."""
        transformer = CellxGeneTransformer()
        result = transformer.transform(self.raw)

        key = list(result.keys())[0]
        entry = result[key]
        self.assertIn("Citation", entry)
        self.assertIsNotNone(entry["Citation"])
        # Citation format: "LastName (Year) Journal"
        self.assertRegex(entry["Citation"], r".+ \(\d{4}\) .+")

    def test_transform_extracts_metadata_fields(self):
        """All expected metadata fields are present."""
        transformer = CellxGeneTransformer()
        result = transformer.transform(self.raw)

        key = list(result.keys())[0]
        entry = result[key]
        expected_fields = [
            "Citation",
            "Link_to_publication",
            "Link_to_CELLxGENE_collection",
            "Link_to_CELLxGENE_dataset",
            "Dataset_name",
            "Number_of_cells",
            "Organism",
            "Tissue",
            "Disease_status",
            "Collection_ID",
            "Collection_version_ID",
            "Dataset_ID",
            "Dataset_version_ID",
        ]
        for field in expected_fields:
            self.assertIn(field, entry, f"Missing field: {field}")

    def test_transform_skips_missing_collection(self):
        """Entries with no collection_json are skipped."""
        transformer = CellxGeneTransformer()
        raw = {"id1": {"dataset_json": {}, "collection_json": None}}
        result = transformer.transform(raw)

        self.assertNotIn("id1", result)


class OpenTargetsTransformerTestCase(unittest.TestCase):
    """Tests for OpenTargetsTransformer using fixture data."""

    def setUp(self):
        with open(FIXTURE_DIR / "opentargets_raw_fixture.json") as f:
            self.raw = json.load(f)
        with open(FIXTURE_DIR / "opentargets_expected_fixture.json") as f:
            self.expected = json.load(f)

    def test_transform_extracts_target_fields(self):
        """Target fields are correctly extracted."""
        transformer = OpenTargetsTransformer()
        result = transformer.transform(self.raw)

        key = [k for k in result if k != "gene_ensembl_ids"][0]
        entry = result[key]
        self.assertIn("target", entry)
        for field in ["id", "approvedSymbol", "approvedName"]:
            self.assertIn(field, entry["target"])

    def test_transform_maps_tractability(self):
        """Tractability resource is mapped from GraphQL response."""
        transformer = OpenTargetsTransformer()
        result = transformer.transform(self.raw)

        key = [k for k in result if k != "gene_ensembl_ids"][0]
        expected_key = [k for k in self.expected][0]

        self.assertIn("tractability", result[key])
        self.assertEqual(
            result[key]["tractability"],
            self.expected[expected_key]["tractability"],
        )

    def test_transform_passes_through_metadata(self):
        """gene_ensembl_ids metadata key is preserved."""
        transformer = OpenTargetsTransformer()
        result = transformer.transform(self.raw)

        self.assertIn("gene_ensembl_ids", result)

    def test_transform_handles_empty_target(self):
        """Entries with missing target get empty resources."""
        transformer = OpenTargetsTransformer()
        raw = {"ENSG_EMPTY": {}}
        result = transformer.transform(raw)

        entry = result["ENSG_EMPTY"]
        self.assertEqual(entry["target"], {})
        self.assertEqual(entry["diseases"], {})


class GeneTransformerTestCase(unittest.TestCase):
    """Tests for GeneTransformer using fixture data."""

    def setUp(self):
        with open(FIXTURE_DIR / "gene_raw_fixture.json") as f:
            self.raw = json.load(f)
        with open(FIXTURE_DIR / "gene_expected_fixture.json") as f:
            self.expected = json.load(f)

    def test_transform_produces_expected_output(self):
        """Transformer output matches old fetcher output."""
        transformer = GeneTransformer()
        result = transformer.transform(self.raw)

        for key in self.expected:
            self.assertIn(key, result)
            self.assertEqual(result[key], self.expected[key])

    def test_transform_extracts_gene_fields(self):
        """Expected gene fields are present in transformed output."""
        transformer = GeneTransformer()
        result = transformer.transform(self.raw)

        key = [k for k in result if k != "gene_entrez_ids"][0]
        entry = result[key]
        expected_fields = [
            "Gene_ID",
            "Official_symbol",
            "Official_full_name",
            "Gene_type",
            "Organism",
            "Summary",
            "UniProt_name",
        ]
        for field in expected_fields:
            self.assertIn(field, entry, f"Missing field: {field}")

    def test_transform_handles_empty_entry(self):
        """Empty entries are preserved as empty dicts."""
        transformer = GeneTransformer()
        raw = {"bad_id": {}, "gene_entrez_ids": ["bad_id"]}
        result = transformer.transform(raw)

        self.assertEqual(result["bad_id"], {})

    def test_transform_passes_through_metadata(self):
        """gene_entrez_ids metadata key is preserved."""
        transformer = GeneTransformer()
        result = transformer.transform(self.raw)

        self.assertIn("gene_entrez_ids", result)


class UniProtTransformerTestCase(unittest.TestCase):
    """Tests for UniProtTransformer using fixture data."""

    def setUp(self):
        with open(FIXTURE_DIR / "uniprot_raw_fixture.json") as f:
            self.raw = json.load(f)
        with open(FIXTURE_DIR / "uniprot_expected_fixture.json") as f:
            self.expected = json.load(f)

    def test_transform_produces_expected_output(self):
        """Transformer output matches old fetcher output."""
        transformer = UniProtTransformer()
        result = transformer.transform(self.raw)

        for key in self.expected:
            self.assertIn(key, result)
            self.assertEqual(result[key], self.expected[key])

    def test_transform_extracts_protein_fields(self):
        """Expected protein fields are present."""
        transformer = UniProtTransformer()
        result = transformer.transform(self.raw)

        key = [k for k in result if k != "protein_accessions"][0]
        entry = result[key]
        expected_fields = [
            "Protein_name",
            "UniProt_ID",
            "Gene_name",
            "Number_of_amino_acids",
            "Function",
            "Annotation_score",
            "Organism",
        ]
        for field in expected_fields:
            self.assertIn(field, entry, f"Missing field: {field}")

    def test_transform_handles_empty_entry(self):
        """Empty entries are preserved as empty dicts."""
        transformer = UniProtTransformer()
        raw = {"BAD": {}, "protein_accessions": ["BAD"]}
        result = transformer.transform(raw)

        self.assertEqual(result["BAD"], {})

    def test_transform_passes_through_metadata(self):
        """protein_accessions metadata key is preserved."""
        transformer = UniProtTransformer()
        result = transformer.transform(self.raw)

        self.assertIn("protein_accessions", result)
