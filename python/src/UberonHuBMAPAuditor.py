"""Report inconsistencies between UBERON and HuBMAP for manual review.

HuBMAP tuples assert ``part_of`` edges and ``label`` annotations over UBERON
terms, and are loaded into the graph after the UBERON ontology triples.  Since
vertex attributes are retained from the last non-None value, a HuBMAP
``ccf_pref_label`` silently overrides the UBERON ``rdfs:label``, and HuBMAP
parents silently add edges the ontology does not assert.

This module compares the two tuple sets and writes a workbook with one sheet
per check, each row carrying a ``Decision`` column, so the differences can be
reviewed and the HuBMAP tuples to retain can be selected:

- Label conflicts: HuBMAP label differs from the UBERON label
- New part_of edges: ``part_of`` asserted by HuBMAP but not by UBERON
- Unknown terms: HuBMAP terms absent from UBERON, or deprecated

The UBERON tuples are those written by ``gov.nih.nlm.OntologyTupleWriter`` from
the UBERON OWL file, so this report sees exactly the triples the ontology graph
builder loads.  The HuBMAP tuples are those written by ``HuBMAPTupleWriter``
into the run's tuples directory.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from LoaderUtilities import (
    DATA_DIRPATH,
    DEPRECATED_TERMS,
    PURLBASE,
    get_current_run,
    set_current_run,
)

# Predicate identifying a part_of relation, as asserted by UBERON (flattened
# from an anonymous subClassOf restriction) and by HuBMAP alike
PART_OF_URI = f"{PURLBASE}/BFO_0000050"

# Predicates carrying a label.  UBERON uses rdfs:label; the tuple writers emit
# the rdf-syntax-ns form.  Both normalize to the "label" vertex attribute in
# OntologyGraphBuilder.parsePredicate, so both are labels here.
LABEL_PREDICATES = frozenset(
    [
        "http://www.w3.org/2000/01/rdf-schema#label",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#label",
    ]
)

# Values offered in the Decision column of each detail sheet
DECISIONS = ["keep-HuBMAP", "keep-UBERON", "defer"]

# Sheet names, in workbook order
SUMMARY_SHEET = "Summary"
LABEL_CONFLICTS_SHEET = "Label conflicts"
NEW_PART_OF_SHEET = "New part_of edges"
UNKNOWN_TERMS_SHEET = "Unknown terms"

# Path to the compiled JAR, relative to the repository root, matching
# flows/_common.py.  Used only by --write-uberon-tuples.
CLASSPATH = os.getenv("CKN_CLASSPATH", "target/nlm-ckn-etl-1.0.jar")

REPO_ROOT = DATA_DIRPATH.parent


def get_audit_dir(run_name=None):
    """Return the audit directory of the named run, creating it if needed.

    The audit directory is deliberately not the run's tuples directory:
    ``ResultsGraphBuilder`` loads every JSON file found there, and these
    ontology tuples are written for review, not for loading.

    Parameters
    ----------
    run_name : str, optional
        Run name.  Defaults to the current run.

    Returns
    -------
    Path
        The audit directory.
    """
    run_name = run_name or get_current_run().run_name
    audit_dir = DATA_DIRPATH / f"audit-{run_name}"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir


def term_of_uri(uri):
    """Return the ontology term named by an OBO PURL, or None.

    Parameters
    ----------
    uri : str
        A tuple element, which may be a PURL or a literal.

    Returns
    -------
    str or None
        The term, in underscore form (e.g. ``UBERON_0002113``), or None if
        the element does not name an OBO term.
    """
    if not isinstance(uri, str) or not uri.startswith(f"{PURLBASE}/"):
        return None
    term = uri[len(PURLBASE) + 1 :]
    return term if "_" in term else None


def curie_of_term(term):
    """Return the CURIE (e.g. ``UBERON:0002113``) of an underscore term."""
    return term.replace("_", ":", 1)


def load_tuple_file(path):
    """Load the tuples from a tuples JSON file.

    Parameters
    ----------
    path : Path
        Path to a JSON file containing a ``tuples`` array.

    Returns
    -------
    list[list]
        The tuples, each a list of three or five elements.
    """
    with open(path, "r") as fp:
        return json.load(fp)["tuples"]


def index_uberon(tuples):
    """Index the labels and part_of edges asserted by UBERON.

    Parameters
    ----------
    tuples : list[list]
        Tuples written from the UBERON OWL file.

    Returns
    -------
    dict
        ``labels`` maps each term to the set of labels asserted for it,
        ``part_of`` maps each subject term to the set of object terms it is
        part of, and ``terms`` is the set of all terms UBERON asserts
        anything about.
    """
    labels = defaultdict(set)
    part_of = defaultdict(set)
    terms = set()

    for tup in tuples:
        if len(tup) != 3:
            continue
        subject, predicate, obj = tup[0], tup[1], tup[2]

        s_term = term_of_uri(subject)
        if s_term is None:
            continue
        terms.add(s_term)

        if predicate in LABEL_PREDICATES:
            labels[s_term].add(obj)
        elif predicate == PART_OF_URI:
            o_term = term_of_uri(obj)
            if o_term is not None:
                part_of[s_term].add(o_term)
                terms.add(o_term)

    return {"labels": dict(labels), "part_of": dict(part_of), "terms": terms}


def index_hubmap(paths):
    """Index the labels and part_of edges asserted by HuBMAP.

    Each label and edge carries the names of the HuBMAP tuple files, that is,
    the organ tables, which assert it.

    Parameters
    ----------
    paths : list[Path]
        Paths to the HuBMAP tuple files of a run.

    Returns
    -------
    dict
        ``labels`` maps each term to a mapping of label to source names,
        ``part_of`` maps each (subject, object) term pair to source names,
        and ``terms`` maps each term to source names.
    """
    labels = defaultdict(lambda: defaultdict(set))
    part_of = defaultdict(set)
    terms = defaultdict(set)

    for path in sorted(paths):
        source = Path(path).stem
        for tup in load_tuple_file(path):
            if len(tup) != 3:
                continue
            subject, predicate, obj = tup[0], tup[1], tup[2]

            s_term = term_of_uri(subject)
            if s_term is None:
                continue
            terms[s_term].add(source)

            if predicate in LABEL_PREDICATES:
                labels[s_term][obj].add(source)
            elif predicate == PART_OF_URI:
                o_term = term_of_uri(obj)
                if o_term is not None:
                    part_of[(s_term, o_term)].add(source)
                    terms[o_term].add(source)

    return {
        "labels": {t: dict(ls) for t, ls in labels.items()},
        "part_of": dict(part_of),
        "terms": dict(terms),
    }


def _join(values):
    """Join a collection of values for display in a cell."""
    return "; ".join(sorted(str(v) for v in values))


def _uberon_label(uberon, term):
    """Return the UBERON label, or labels, of a term, for display."""
    return _join(uberon["labels"].get(term, set()))


def _display_label(uberon, hubmap, term):
    """Return the UBERON label of a term, falling back to the HuBMAP label."""
    label = _uberon_label(uberon, term)
    if label:
        return label
    return _join(hubmap["labels"].get(term, {}).keys())


def find_label_conflicts(uberon, hubmap):
    """Find terms HuBMAP labels differently than UBERON does.

    A HuBMAP label overrides the UBERON label on load, so every row here is a
    label the graph would take from HuBMAP.  Terms UBERON asserts no label for
    are left to :func:`find_unknown_terms`.

    Parameters
    ----------
    uberon : dict
        Index returned by :func:`index_uberon`.
    hubmap : dict
        Index returned by :func:`index_hubmap`.

    Returns
    -------
    pandas.DataFrame
        One row per conflicting (term, HuBMAP label) pair.
    """
    rows = []
    for term, hubmap_labels in hubmap["labels"].items():
        uberon_labels = uberon["labels"].get(term)
        if not uberon_labels:
            continue
        for hubmap_label, sources in hubmap_labels.items():
            if hubmap_label in uberon_labels:
                continue
            normalized = {label.strip().casefold() for label in uberon_labels}
            differs_by = (
                "case-or-whitespace"
                if hubmap_label.strip().casefold() in normalized
                else "substantive"
            )
            rows.append(
                {
                    "term": curie_of_term(term),
                    "uberon_label": _join(uberon_labels),
                    "hubmap_label": hubmap_label,
                    "differs_by": differs_by,
                    "hubmap_source": _join(sources),
                    "Decision": "",
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "term",
            "uberon_label",
            "hubmap_label",
            "differs_by",
            "hubmap_source",
            "Decision",
        ],
    ).sort_values(
        # Substantive conflicts first: a differing name needs a decision, where
        # a differing capitalization usually does not
        ["differs_by", "term"],
        ascending=[False, True],
        ignore_index=True,
    )


def find_new_part_of_edges(uberon, hubmap):
    """Find part_of edges HuBMAP asserts but UBERON does not.

    The parents UBERON does assert for the subject term are carried along, so
    a reviewer can see whether the HuBMAP edge complements or contradicts the
    ontology.

    Parameters
    ----------
    uberon : dict
        Index returned by :func:`index_uberon`.
    hubmap : dict
        Index returned by :func:`index_hubmap`.

    Returns
    -------
    pandas.DataFrame
        One row per HuBMAP-only edge.
    """
    rows = []
    for (s_term, o_term), sources in hubmap["part_of"].items():
        uberon_parents = uberon["part_of"].get(s_term, set())
        if o_term in uberon_parents:
            continue
        rows.append(
            {
                "subject_term": curie_of_term(s_term),
                "subject_label": _display_label(uberon, hubmap, s_term),
                "object_term": curie_of_term(o_term),
                "object_label": _display_label(uberon, hubmap, o_term),
                "uberon_parents": _join(
                    f"{curie_of_term(p)} ({_uberon_label(uberon, p)})"
                    for p in uberon_parents
                ),
                "hubmap_source": _join(sources),
                "Decision": "",
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "subject_term",
            "subject_label",
            "object_term",
            "object_label",
            "uberon_parents",
            "hubmap_source",
            "Decision",
        ],
    ).sort_values(["subject_term", "object_term"], ignore_index=True)


def find_unknown_terms(uberon, hubmap, deprecated_terms=None):
    """Find HuBMAP terms UBERON does not contain, or which are deprecated.

    Parameters
    ----------
    uberon : dict
        Index returned by :func:`index_uberon`.
    hubmap : dict
        Index returned by :func:`index_hubmap`.
    deprecated_terms : collection, optional
        Terms deprecated by the ontologies, in underscore form.  Defaults to
        ``LoaderUtilities.DEPRECATED_TERMS``.

    Returns
    -------
    pandas.DataFrame
        One row per (term, reason) pair.
    """
    if deprecated_terms is None:
        deprecated_terms = DEPRECATED_TERMS
    deprecated_terms = set(deprecated_terms)

    rows = []
    for term, sources in hubmap["terms"].items():
        reasons = []
        if term not in uberon["terms"]:
            reasons.append("absent-from-uberon")
        if term in deprecated_terms:
            reasons.append("deprecated")
        for reason in reasons:
            rows.append(
                {
                    "term": curie_of_term(term),
                    "hubmap_label": _join(hubmap["labels"].get(term, {}).keys()),
                    "reason": reason,
                    "hubmap_source": _join(sources),
                    "Decision": "",
                }
            )
    return pd.DataFrame(
        rows,
        columns=["term", "hubmap_label", "reason", "hubmap_source", "Decision"],
    ).sort_values(["reason", "term"], ignore_index=True)


def _format_sheet(worksheet, data_frame):
    """Freeze the header row, size the columns, and offer the decisions."""
    worksheet.freeze_panes = "A2"

    for i_column, column in enumerate(data_frame.columns, start=1):
        letter = get_column_letter(i_column)
        widths = [len(str(column))] + [
            len(str(value)) for value in data_frame[column].head(200)
        ]
        worksheet.column_dimensions[letter].width = min(max(widths) + 2, 60)

        if column == "Decision" and len(data_frame) > 0:
            validation = DataValidation(
                type="list",
                formula1='"' + ",".join(DECISIONS) + '"',
                allow_blank=True,
            )
            worksheet.add_data_validation(validation)
            validation.add(f"{letter}2:{letter}{len(data_frame) + 1}")


def write_workbook(sheets, output_path, summary=None):
    """Write the review workbook, one sheet per check.

    Parameters
    ----------
    sheets : dict[str, pandas.DataFrame]
        Sheet name to rows, in workbook order.
    output_path : Path
        Path to the output XLSX file.
    summary : list[tuple], optional
        Rows of (name, value) describing the inputs, written to a leading
        summary sheet along with the row count of each sheet.

    Returns
    -------
    Path
        The output path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary_rows = list(summary or [])
    summary_rows.extend((f"{name} rows", len(rows)) for name, rows in sheets.items())
    summary_frame = pd.DataFrame(summary_rows, columns=["Item", "Value"])

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_frame.to_excel(writer, sheet_name=SUMMARY_SHEET, index=False)
        _format_sheet(writer.sheets[SUMMARY_SHEET], summary_frame)
        for name, rows in sheets.items():
            rows.to_excel(writer, sheet_name=name, index=False)
            _format_sheet(writer.sheets[name], rows)

    print(f"Wrote {output_path}")
    return output_path


def audit(uberon_tuples_path, hubmap_tuple_paths, output_path):
    """Compare the UBERON and HuBMAP tuples, and write the review workbook.

    Parameters
    ----------
    uberon_tuples_path : Path
        Path to the tuples written from the UBERON OWL file.
    hubmap_tuple_paths : list[Path]
        Paths to the HuBMAP tuple files of a run.
    output_path : Path
        Path to the output XLSX file.

    Returns
    -------
    dict[str, pandas.DataFrame]
        The sheets written, keyed by sheet name.
    """
    uberon = index_uberon(load_tuple_file(uberon_tuples_path))
    hubmap = index_hubmap(hubmap_tuple_paths)
    print(
        f"Indexed {len(uberon['terms'])} UBERON terms, "
        f"and {len(hubmap['terms'])} HuBMAP terms "
        f"from {len(hubmap_tuple_paths)} file(s)"
    )

    sheets = {
        LABEL_CONFLICTS_SHEET: find_label_conflicts(uberon, hubmap),
        NEW_PART_OF_SHEET: find_new_part_of_edges(uberon, hubmap),
        UNKNOWN_TERMS_SHEET: find_unknown_terms(uberon, hubmap),
    }
    for name, rows in sheets.items():
        print(f"{name}: {len(rows)}")

    summary = [
        ("UBERON tuples", str(uberon_tuples_path)),
        ("HuBMAP tuple files", len(hubmap_tuple_paths)),
        ("UBERON terms", len(uberon["terms"])),
        ("HuBMAP terms", len(hubmap["terms"])),
    ]
    write_workbook(sheets, output_path, summary=summary)
    return sheets


def write_uberon_tuples(output_path, java_opts=None):
    """Run the Java ontology tuple writer to produce the UBERON tuples.

    Parameters
    ----------
    output_path : Path
        Path to the tuples JSON file to write.
    java_opts : str, optional
        JVM flags.  Defaults to ``$CKN_JAVA_OPTS`` or ``-Xmx32g``.
    """
    java_opts = java_opts or os.getenv("CKN_JAVA_OPTS", "-Xmx32g")
    command = (
        ["java"]
        + java_opts.split()
        + [
            "-cp",
            CLASSPATH,
            "gov.nih.nlm.OntologyTupleWriter",
            "--output",
            str(output_path),
        ]
    )
    print(f"Writing UBERON tuples: {' '.join(command)}")
    subprocess.run(command, check=True, cwd=REPO_ROOT)


def main():
    """Run the UBERON and HuBMAP audit."""
    parser = argparse.ArgumentParser(
        description="Report inconsistencies between UBERON and HuBMAP for manual review"
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="run name (selects data/tuples-<name>/; defaults to $CKN_RUN or 'full')",
    )
    parser.add_argument(
        "--uberon-tuples",
        default=None,
        help="path to the UBERON tuples JSON file (default: data/audit-<name>/uberon-tuples.json)",
    )
    parser.add_argument(
        "--tuples-dir",
        default=None,
        help="directory containing the HuBMAP tuple files (default: data/tuples-<name>/)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="path to the output workbook (default: data/audit-<name>/uberon-hubmap-review.xlsx)",
    )
    parser.add_argument(
        "--write-uberon-tuples",
        action="store_true",
        help=(
            "run gov.nih.nlm.OntologyTupleWriter to produce the UBERON tuples "
            "from data/obo/uberon*.owl if they are missing"
        ),
    )
    args = parser.parse_args()
    run = set_current_run(args.run_name)

    audit_dir = get_audit_dir(run.run_name)
    uberon_tuples_path = Path(args.uberon_tuples or audit_dir / "uberon-tuples.json")
    if not uberon_tuples_path.exists():
        if not args.write_uberon_tuples:
            print(
                f"UBERON tuples not found at {uberon_tuples_path}\n"
                "Run gov.nih.nlm.OntologyTupleWriter, or pass --write-uberon-tuples"
            )
            return 1
        write_uberon_tuples(uberon_tuples_path)

    tuples_dir = Path(args.tuples_dir or run.tuples_dir)
    hubmap_tuple_paths = sorted(tuples_dir.glob("hubmap-*.json"))
    if not hubmap_tuple_paths:
        print(
            f"No HuBMAP tuple files found in {tuples_dir}\n"
            "Run HuBMAPTupleWriter.py, or TupleWriterPipeline.py, first"
        )
        return 1

    output_path = Path(args.output or audit_dir / "uberon-hubmap-review.xlsx")
    audit(uberon_tuples_path, hubmap_tuple_paths, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
