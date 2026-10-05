import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from CellxGeneTupleWriter import create_tuples


class CellxGeneTupleWriterTestCase(unittest.TestCase):
    """Tests for CellxGeneTupleWriter.create_tuples."""

    def _make_dataset(self, dvid, doi):
        return {
            "Dataset_name": f"Dataset {dvid}",
            "Organism": "Homo sapiens",
            "Tissue": "brain",
            "Disease_status": "normal",
            "Number_of_cells": 10000,
            "Citation": "Smith (2024) Nature",
            "Link_to_publication": doi,
            "Link_to_CELLxGENE_collection": "https://cellxgene.cziscience.com/collections/abc",
            "Link_to_CELLxGENE_dataset": f"https://cellxgene.cziscience.com/e/{dvid}.cxg/",
            "Collection_ID": "abc",
            "Collection_version_ID": "cv-001",
            "Dataset_ID": f"ds-{dvid}",
            "Dataset_version_ID": dvid,
        }

    def _make_data(self):
        return {"dvid-001": self._make_dataset("dvid-001", "https://doi.org/10.1234/test")}

    def _make_multi_dataset_data(self):
        # Two datasets of one paper, plus a third dataset of another paper.
        return {
            "dvid-001": self._make_dataset("dvid-001", "https://doi.org/10.1234/test"),
            "dvid-002": self._make_dataset("dvid-002", "https://doi.org/10.1234/test"),
            "dvid-003": self._make_dataset("dvid-003", "https://doi.org/10.5678/other"),
        }

    def _attributed_to_edges(self, tuples):
        """Return the (subject, object) terms of the was_attributed_to edges."""
        return {
            (str(t[0]).rsplit("/", 1)[-1], str(t[2]).rsplit("/", 1)[-1])
            for t in tuples
            if str(t[1]).endswith("wasAttributedTo")
        }

    def _annotations(self, tuples, prefix, attribute):
        """Return the values of an annotation of the vertices of a collection."""
        return [
            str(t[2])
            for t in tuples
            if len(t) == 3
            and str(t[0]).rsplit("/", 1)[-1].startswith(prefix)
            and str(t[1]).endswith(f"#{attribute}")
        ]

    def test_creates_tuples(self):
        tuples = create_tuples(self._make_data())
        self.assertGreater(len(tuples), 0)

    def test_no_organ_map_keeps_source_keyed_vertex(self):
        # Without a dataset->organs map, the vertex keys on the source id.
        tuples = create_tuples(self._make_data())
        subjects = {s for s, _ in self._attributed_to_edges(tuples)}
        self.assertEqual(subjects, {"CSD_dvid-001"})

    def test_fans_out_one_vertex_per_organ(self):
        # A source filtered for two organs is attributed to the publication
        # once per organ, each with a composite (source__organ) key.
        dataset_organs = {"dvid-001": {"kidney", "liver"}}
        tuples = create_tuples(self._make_data(), dataset_organs)
        subjects = {s for s, _ in self._attributed_to_edges(tuples)}
        self.assertEqual(
            subjects, {"CSD_dvid-001__kidney", "CSD_dvid-001__liver"}
        )

    def test_fan_out_matches_summary_driven_key(self):
        # The composite key here must equal the one build_cell_set_dataset
        # produces from a summary, so the two writers share the vertex.
        from TupleWriterUtilities import cell_set_dataset_identifier

        dataset_organs = {"dvid-001": {"kidney"}}
        tuples = create_tuples(self._make_data(), dataset_organs)
        subjects = {s for s, _ in self._attributed_to_edges(tuples)}
        self.assertEqual(
            subjects, {f"CSD_{cell_set_dataset_identifier('dvid-001', 'kidney')}"}
        )

    def test_contains_source_quintuple(self):
        tuples = create_tuples(self._make_data())
        quints = [t for t in tuples if len(t) == 5 and "Source" in str(t[3])]
        self.assertTrue(any("CELLxGENE" in str(t[4]) for t in quints))

    def test_csd_annotations(self):
        tuples = create_tuples(self._make_data())
        csd_annots = [
            t for t in tuples
            if len(t) == 3 and "CSD_" in str(t[0])
        ]
        self.assertGreater(len(csd_annots), 0)

    def test_dataset_named_by_citation_and_dataset_name(self):
        tuples = create_tuples(self._make_data())
        self.assertEqual(
            self._annotations(tuples, "CSD_", "Name"),
            ["Smith (2024) Nature - Dataset dvid-001"],
        )

    def test_datasets_of_one_paper_are_named_distinctly(self):
        # The three datasets share a citation, so only the dataset name
        # tells them apart.
        tuples = create_tuples(self._make_multi_dataset_data())
        names = self._annotations(tuples, "CSD_", "Name")
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)

    def test_citation_and_dataset_name_are_kept(self):
        # The name is an additional annotation: the citation still links
        # to the publication, and the dataset name is still displayed.
        tuples = create_tuples(self._make_data())
        self.assertEqual(
            self._annotations(tuples, "CSD_", "Citation"), ["Smith (2024) Nature"]
        )
        self.assertEqual(
            self._annotations(tuples, "CSD_", "dataset_name"), ["Dataset dvid-001"]
        )
        self.assertEqual(
            self._annotations(tuples, "PUB_", "Citation"), ["Smith (2024) Nature"]
        )

    def test_publication_is_not_named(self):
        # Name identifies a dataset, so it belongs on the CSD alone.
        tuples = create_tuples(self._make_data())
        self.assertEqual(self._annotations(tuples, "PUB_", "Name"), [])

    def test_publication_keyed_by_doi(self):
        tuples = create_tuples(self._make_data())
        self.assertEqual(
            self._attributed_to_edges(tuples),
            {("CSD_dvid-001", "PUB_10.1234-test")},
        )

    def test_datasets_of_one_paper_share_one_publication(self):
        tuples = create_tuples(self._make_multi_dataset_data())
        self.assertEqual(
            self._attributed_to_edges(tuples),
            {
                ("CSD_dvid-001", "PUB_10.1234-test"),
                ("CSD_dvid-002", "PUB_10.1234-test"),
                ("CSD_dvid-003", "PUB_10.5678-other"),
            },
        )

    def test_distinct_papers_stay_distinct(self):
        tuples = create_tuples(self._make_multi_dataset_data())
        pub_terms = {
            str(t[0]).rsplit("/", 1)[-1]
            for t in tuples
            if str(t[0]).rsplit("/", 1)[-1].startswith("PUB_")
        }
        self.assertEqual(pub_terms, {"PUB_10.1234-test", "PUB_10.5678-other"})

    def test_publication_doi_annotation_is_a_bare_doi(self):
        tuples = create_tuples(self._make_data())
        dois = [
            str(t[2])
            for t in tuples
            if len(t) == 3
            and str(t[0]).endswith("PUB_10.1234-test")
            and str(t[1]).endswith("publication_doi")
        ]
        self.assertEqual(dois, ["10.1234/test"])

    def test_is_reference_dataset_true_when_in_the_reference_set(self):
        tuples = create_tuples(
            self._make_data(), reference_dataset_version_ids={"dvid-001"}
        )
        values = self._annotations(tuples, "CSD_", "is_reference_dataset")
        self.assertEqual(values, ["True"])

    def test_is_reference_dataset_false_explicitly_when_not_in_the_set(self):
        # Explicit False, not left unset: a CSD not backed by a
        # cluster_cid_mapping file must still assert the field
        # (Springbok-LLC/nlm-ckn-etl#122).
        tuples = create_tuples(
            self._make_data(), reference_dataset_version_ids={"some-other-dvid"}
        )
        values = self._annotations(tuples, "CSD_", "is_reference_dataset")
        self.assertEqual(values, ["False"])

    def test_is_reference_dataset_false_when_no_reference_set_given(self):
        tuples = create_tuples(self._make_data())
        values = self._annotations(tuples, "CSD_", "is_reference_dataset")
        self.assertEqual(values, ["False"])


if __name__ == "__main__":
    unittest.main()
