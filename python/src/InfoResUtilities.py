"""Map NLM-CKN edge ``Source`` values to Biolink information resource CURIEs.

Biolink provenance slots (``primary_knowledge_source``, ``provided_by``, ...)
expect ``infores:`` CURIEs from the Information Resource Registry,
https://github.com/biolink/information-resource-registry
(``infores_catalog.yaml``).  Every CURIE below except ``NLM_CKN_INFORES`` was
checked against that catalog on 2026-09-10.

Deliberately dependency-free so any loader or exporter can import it.
"""

# NLM-CKN itself.  Not a registered CURIE.
NLM_CKN_INFORES = "infores:nlm-ckn"

# Keyed by upper-cased Source: OntologyGraphBuilder.normalizeEdgeSource
# upper-cases ontology names, while the tuple writers pass mixed case.
_SOURCE_TO_INFORES = {
    # Ontologies, as named by OntologyGraphBuilder.normalizeEdgeSource.
    "CL": "infores:cl",
    "GO": "infores:go",
    "HP": "infores:hpo",
    "HSAPDV": "infores:hsapdv",
    "MONDO": "infores:mondo",
    "NCBITAXON": "infores:ncbi-taxon",
    "PATO": "infores:pato",
    "PR": "infores:pr",
    "UBERON": "infores:uberon",
    # Tuple writers, as passed to association_to_tuples(source=...).
    "OPEN TARGETS": "infores:open-targets",
    # Drug-protein edges: Open Targets asserts the interaction; NCBI Gene
    # only supplies the gene name.
    "OPEN TARGETS AND GENE": "infores:open-targets",
    "UNIPROT": "infores:uniprot",
    # NLM-CKN's own assertions: NS-Forest is the algorithm NLM-CKN runs, and
    # Manual Mapping is its curation.
    "NS-FOREST": NLM_CKN_INFORES,
    "MANUAL MAPPING": NLM_CKN_INFORES,
    # CELLxGENE is deliberately absent: an outside resource with no
    # registered CURIE, which NLM-CKN should not claim.
}


def source_to_infores(source: str) -> str | None:
    """Return the infores CURIE for an edge ``Source`` value.

    Matching ignores case and surrounding whitespace.  Returns ``None`` for an
    empty or unknown source, and for CELLxGENE, which has no registered CURIE;
    callers choose their own fallback.

    Parameters
    ----------
    source:
        A single ``Source`` value, e.g. ``"NS-Forest"`` or ``"MONDO"``.
    """
    if not source:
        return None
    return _SOURCE_TO_INFORES.get(source.strip().upper())
