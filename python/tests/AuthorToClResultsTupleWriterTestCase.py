import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from AuthorToClResultsTupleWriter import create_tuples_from_author_to_cl

SUMMARIES_DIRPATH = Path(__file__).parents[2] / "src" / "test" / "data" / "summaries"


def to_string_tuples(tuples):
    """Convert list of URIRef/Literal tuples to list of string lists."""
    return [list(str(x) for x in t) for t in tuples]


class CreateTuplesFromAuthorToClTestCase(unittest.TestCase):
    """Tests for create_tuples_from_author_to_cl using summary fixtures."""

    def setUp(self):
        author_to_cl_path = SUMMARIES_DIRPATH / "nlm-ckn-map-author-to-cl-li-2023.json"
        with open(author_to_cl_path, "r") as fp:
            self.author_to_cl_summary = json.load(fp)
        self.results_df = pd.DataFrame(self.author_to_cl_summary["results"])
        self.expected_tuples = self.author_to_cl_summary["tuples"]
        self.dataset_version_ids = (
            self.results_df["dataset_version_id"].unique().tolist()
        )

    def test_create_tuples_from_author_to_cl(self):
        """Tuples created from summary results match expected tuples."""
        actual_tuples = create_tuples_from_author_to_cl(
            self.results_df, self.dataset_version_ids, {}
        )
        actual_as_strings = to_string_tuples(actual_tuples)
        self.assertEqual(actual_as_strings, self.expected_tuples)

    def test_tuple_count(self):
        """Number of tuples matches expected count."""
        actual_tuples = create_tuples_from_author_to_cl(
            self.results_df, self.dataset_version_ids, {}
        )
        self.assertEqual(len(actual_tuples), len(self.expected_tuples))

    def test_first_tuple_is_cl_part_of_uberon(self):
        """First tuple is a CL PART_OF UBERON relation."""
        actual_tuples = create_tuples_from_author_to_cl(
            self.results_df, self.dataset_version_ids, {}
        )
        first = list(str(x) for x in actual_tuples[0])
        self.assertIn("CL_", first[0])
        self.assertIn("BFO_0000050", first[1])
        self.assertIn("UBERON_", first[2])

    def test_last_tuple_is_gene_source(self):
        """Last tuple is a Gene PART_OF Cell type Source edge annotation."""
        actual_tuples = create_tuples_from_author_to_cl(
            self.results_df, self.dataset_version_ids, {}
        )
        last = list(str(x) for x in actual_tuples[-1])
        self.assertIn("GS_ITIH5", last[0])
        self.assertIn("BFO_0000050", last[1])
        self.assertIn("CL_4030027", last[2])
        self.assertIn("Source", last[3])
        self.assertEqual(last[4], "NSForest")
