"""Create tuples from HuBMAP data using schema entities.

Produces AnatomicalStructure part of AnatomicalStructure and CellType part of
AnatomicalStructure associations from HuBMAP CCF data tables.
"""

import json
from glob import glob
from pathlib import Path

from ckn_schema.pydantic.ckn_schema import AnatomicalStructure, CellType

from LoaderUtilities import (
    DEPRECATED_TERMS,
    get_cl_terms,
    get_current_run,
    get_dataset_file_paths,
    get_results_sources,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    association_to_tuples,
    get_tuples_dir,
    write_tuples,
)


def create_tuples(hubmap_data: dict, cl_terms: set[str]) -> list[tuple]:
    """Create tuples from HuBMAP data.

    Produces:
    - AnatomicalStructurePartOfAnatomicalStructure
    - CellTypePartOfAnatomicalStructure

    Parameters
    ----------
    hubmap_data : dict
        Dictionary containing HuBMAP CCF data with 'data' key
        containing 'anatomical_structures' and 'cell_types' arrays.
        Each anatomical structure has 'id', 'ccf_pref_label', and
        'ccf_part_of'. Each cell type has 'id', 'ccf_pref_label',
        and 'ccf_located_in'.
    cl_terms : set[str]
        Set of CL terms (e.g., 'CL_0000235') from author-to-CL
        mapping files. Only cell types with terms in this set are
        included.

    Returns
    -------
    list[tuple]
        List of 3-element and 5-element RDF tuples.
    """
    tuples = []

    # AnatomicalStructure part_of AnatomicalStructure
    for anat_struct in hubmap_data.get("data", {}).get("anatomical_structures", []):
        if "id" not in anat_struct or "ccf_part_of" not in anat_struct:
            print("Warning: Anatomical structure missing 'id' or 'ccf_part_of' keys")
            continue

        s_uberon_term = anat_struct["id"].replace(":", "_")
        if "UBERON" not in s_uberon_term:
            continue
        if s_uberon_term in DEPRECATED_TERMS:
            print(f"Warning: UBERON term {s_uberon_term} deprecated")

        subject = AnatomicalStructure(
            ontology_purl=anat_struct["id"],
            label=anat_struct.get("ccf_pref_label"),
        )

        for o_id in anat_struct["ccf_part_of"]:
            if "UBERON" not in o_id:
                continue
            o_uberon_term = o_id.replace(":", "_")
            if o_uberon_term in DEPRECATED_TERMS:
                print(f"Warning: UBERON term {o_uberon_term} deprecated")

            obj = AnatomicalStructure(ontology_purl=o_id)
            assoc = ASSOCIATION_CLASSES["AnatomicalStructurePartOfAnatomicalStructure"](
                subject=subject,
                predicate="part_of",
                object=obj,
            )
            tuples.extend(association_to_tuples(assoc, source="HuBMAP"))

    # CellType part_of AnatomicalStructure
    for cell_type in hubmap_data.get("data", {}).get("cell_types", []):
        if "id" not in cell_type or "ccf_located_in" not in cell_type:
            print("Warning: Cell type missing 'id' or 'ccf_located_in' keys")
            continue

        cl_term = cell_type["id"].replace(":", "_")
        if "CL" not in cl_term or "PCL" in cl_term:
            continue
        if cl_term not in cl_terms:
            continue

        ct_curie = cell_type["id"]
        subject = CellType(
            ontology_purl=ct_curie,
            label=cell_type.get("ccf_pref_label"),
        )

        for uberon_id in cell_type["ccf_located_in"]:
            if "UBERON" not in uberon_id:
                continue
            uberon_term = uberon_id.replace(":", "_")
            if uberon_term in DEPRECATED_TERMS:
                print(f"Warning: UBERON term {uberon_term} deprecated")

            obj = AnatomicalStructure(ontology_purl=uberon_id)
            assoc = ASSOCIATION_CLASSES["CellTypePartOfAnatomicalStructure"](
                subject=subject,
                predicate="part_of",
                object=obj,
            )
            tuples.extend(association_to_tuples(assoc, source="HuBMAP"))

    return tuples


def main():
    """Run HuBMAP tuple writer.

    Loads CL terms from mapping files, then creates tuples from each
    HuBMAP JSON data file. Writes one JSON tuple file per HuBMAP
    source file.
    """
    hubmap_dir = get_current_run().external_dir / "hubmap"
    if not hubmap_dir.exists():
        print(f"HuBMAP data not found at {hubmap_dir}")
        return

    # Get CL terms from mapping files
    results_sources = get_results_sources()
    file_paths = get_dataset_file_paths(results_sources)
    cl_terms = get_cl_terms(file_paths["mapping_paths"])

    tuples_dir = get_tuples_dir()
    hubmap_paths = [Path(p).resolve() for p in glob(str(hubmap_dir / "*.json"))]
    for hubmap_path in hubmap_paths:
        print(f"Creating HuBMAP tuples from {hubmap_path.name}")
        with open(hubmap_path, "r") as fp:
            hubmap_data = json.load(fp)

        tuples = create_tuples(hubmap_data, cl_terms)
        if tuples:
            output_name = f"hubmap-{hubmap_path.name}"
            write_tuples(tuples, tuples_dir / output_name)


if __name__ == "__main__":
    main()
