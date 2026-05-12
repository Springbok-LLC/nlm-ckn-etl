"""Create tuples from HuBMAP data using schema entities.

Produces AnatomicalStructure part of AnatomicalStructure associations from
HuBMAP CCF data tables.
"""

import json
from glob import glob
from pathlib import Path

from ckn_schema.pydantic.ckn_schema import AnatomicalStructure

from LoaderUtilities import (
    DEPRECATED_TERMS,
    get_current_run,
)

from TupleWriterUtilities import (
    ASSOCIATION_CLASSES,
    association_to_tuples,
    get_tuples_dir,
    write_tuples,
)


def create_tuples(hubmap_data: dict) -> list[tuple]:
    """Create tuples from HuBMAP data.

    Produces:
    - AnatomicalStructurePartOfAnatomicalStructure

    Parameters
    ----------
    hubmap_data : dict
        Dictionary containing HuBMAP CCF data with 'data' key
        containing an 'anatomical_structures' array. Each anatomical
        structure has 'id', 'ccf_pref_label', and 'ccf_part_of'.

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
                predicate="nlm-ckn:part_of",
                object=obj,
            )
            tuples.extend(association_to_tuples(assoc, source="HuBMAP"))

    return tuples


def main():
    """Run HuBMAP tuple writer.

    Creates tuples from each HuBMAP JSON data file. Writes one JSON
    tuple file per HuBMAP source file.
    """
    hubmap_dir = get_current_run().external_dir / "hubmap"
    if not hubmap_dir.exists():
        print(f"HuBMAP data not found at {hubmap_dir}")
        return

    tuples_dir = get_tuples_dir()
    hubmap_paths = [Path(p).resolve() for p in glob(str(hubmap_dir / "*.json"))]
    for hubmap_path in hubmap_paths:
        print(f"Creating HuBMAP tuples from {hubmap_path.name}")
        with open(hubmap_path, "r") as fp:
            hubmap_data = json.load(fp)

        tuples = create_tuples(hubmap_data)
        if tuples:
            output_name = f"hubmap-{hubmap_path.name}"
            write_tuples(tuples, tuples_dir / output_name)


if __name__ == "__main__":
    main()
