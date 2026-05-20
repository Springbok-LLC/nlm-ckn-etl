"""
Session-scoped patch that replaces the live BioMart query with a small
in-memory fixture.  Every test that indirectly calls
get_gene_names_and_ensembl_and_entrez_ids() (via the various map helpers)
will receive this data instead of hitting the network.

TODO: Add a contract test (tests/contract/test_biomart_contract.py) that makes
a real BioMart call and asserts the expected columns are present. Run it on a
schedule (nightly/weekly) rather than on every push so upstream API changes
(e.g. attribute renames like entrezgene_id -> ncbi_gene_id) are caught early
with a clear error message instead of a cryptic KeyError in production.
"""
from unittest.mock import patch

import pandas as pd
import pytest


# Covers all gene names and Entrez IDs referenced across the test suite fixtures.
_GENE_MAPPING = pd.DataFrame(
    {
        "external_gene_name": [
            "BRCA1", "BRCA2", "CDH2", "CFTR", "EGFR", "KRAS", "MYC", "TP53",
        ],
        "ensembl_gene_id": [
            "ENSG00000012048",
            "ENSG00000139618",
            "ENSG00000170558",
            "ENSG00000001626",
            "ENSG00000146648",
            "ENSG00000133703",
            "ENSG00000136997",
            "ENSG00000141510",
        ],
        "entrezgene_id": ["672", "675", "1000", "1080", "1956", "3845", "4609", "7157"],
    }
)


@pytest.fixture(autouse=True, scope="session")
def mock_biomart():
    """Patch get_gene_names_and_ensembl_and_entrez_ids for the entire test
    session so no test ever makes a live BioMart network call."""
    with patch(
        "LoaderUtilities.get_gene_names_and_ensembl_and_entrez_ids",
        return_value=_GENE_MAPPING.copy(),
    ):
        yield
