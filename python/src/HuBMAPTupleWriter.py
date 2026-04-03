"""Create tuples from HuBMAP data using schema entities.

Produces AnatomicalStructure → AnatomicalStructure (part_of) and
CellType → AnatomicalStructure (part_of) associations from HuBMAP
CCF data tables.
"""

import json
from glob import glob
from pathlib import Path

from ckn_schema.pydantic.ckn_schema import (
    AnatomicalStructure,
    CellType,
)

from ExternalApiResultsFetcher import HUBMAP_DIRPATH

from LoaderUtilities import (
    DEPRECATED_TERMS,
    get_cl_terms,
    get_dataset_file_paths,
    get_results_sources,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    TUPLES_DIRPATH,
    association_to_tuples,
    write_tuples,
)


def create_tuples(hubmap_data: dict, cl_terms: set[str]) -> list[tuple]:
    """Create tuples from HuBMAP data.

    Produces:
    - AnatomicalStructurePartOfAnatomicalStructure
    - CellTypePartOfAnatomicalStructure
    """
    tuples = []

    # AnatomicalStructure part_of AnatomicalStructure
    for anat_struct in hubmap_data.get("data", {}).get("anatomical_structures", []):
        if "id" not in anat_struct or "ccf_part_of" not in anat_struct:
            continue

        s_uberon_term = anat_struct["id"].replace(":", "_")
        if "UBERON" not in s_uberon_term:
            continue
        if s_uberon_term in DEPRECATED_TERMS:
            print(f"Warning: UBERON term {s_uberon_term} deprecated")

        s_curie = anat_struct["id"]
        subject = AnatomicalStructure(
            ontology_purl=s_curie,
            label=anat_struct.get("ccf_pref_label"),
        )

        for o_id in anat_struct["ccf_part_of"]:
            if "UBERON" not in o_id:
                continue
            o_uberon_term = o_id.replace(":", "_")
            if o_uberon_term in DEPRECATED_TERMS:
                print(f"Warning: UBERON term {o_uberon_term} deprecated")

            obj = AnatomicalStructure(ontology_purl=o_id)
            assoc = ASSOCIATION_CLASSES[
                "AnatomicalStructurePartOfAnatomicalStructure"
            ](
                subject=subject, predicate="part_of", object=obj,
            )
            tuples.extend(association_to_tuples(assoc, source="HuBMAP"))

    # CellType part_of AnatomicalStructure
    for cell_type in hubmap_data.get("data", {}).get("cell_types", []):
        if "id" not in cell_type or "ccf_located_in" not in cell_type:
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
                subject=subject, predicate="part_of", object=obj,
            )
            tuples.extend(association_to_tuples(assoc, source="HuBMAP"))

    return tuples


def main():
    """Run HuBMAP tuple writer."""
    if not HUBMAP_DIRPATH.exists():
        print(f"HuBMAP data not found at {HUBMAP_DIRPATH}")
        return

    # Get CL terms from mapping files
    results_sources = get_results_sources()
    file_paths = get_dataset_file_paths(results_sources)
    cl_terms = get_cl_terms(file_paths["mapping_paths"])

    hubmap_paths = [Path(p).resolve() for p in glob(str(HUBMAP_DIRPATH / "*.json"))]
    for hubmap_path in hubmap_paths:
        print(f"Creating HuBMAP tuples from {hubmap_path.name}")
        with open(hubmap_path, "r") as fp:
            hubmap_data = json.load(fp)

        tuples = create_tuples(hubmap_data, cl_terms)
        if tuples:
            output_name = f"hubmap-{hubmap_path.name}"
            write_tuples(tuples, TUPLES_DIRPATH / output_name)


if __name__ == "__main__":
    main()
