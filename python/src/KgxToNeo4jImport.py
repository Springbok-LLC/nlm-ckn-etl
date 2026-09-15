"""Convert one database's KGX TSV export to ``neo4j-admin database import`` CSV.

Reads ``<src_dir>/<db_name>_nodes.tsv`` and ``<src_dir>/<db_name>_edges.tsv``
(the pair ``export_kgx`` writes into ``runs/{run}/07-kgx.tar.gz``) and writes
``<dst_dir>/nodes.csv`` and ``<dst_dir>/edges.csv``, to be imported with
``--array-delimiter="|" --multiline-fields=true``.

The graph model mirrors ``kgx neo4j-upload``:

- every node is labelled ``biolink:NamedThing`` plus one label per
  pipe-separated ``category``, and ``category`` is kept as a ``string[]``
- the relationship type is the ``predicate`` column
- every other column is a string property, with ``:`` in its name replaced by
  ``_`` (a ``:`` in an import header would be read as a type suffix)

Empty cells are written unquoted, which the importer treats as absent, so a
node or edge only carries the properties it has a value for. Values are
otherwise copied verbatim, including any double quotes.

Deliberately dependency-free: src/main/docker/neo4j/Dockerfile runs it in a
bare python image.

Usage: python KgxToNeo4jImport.py <src_dir> <dst_dir> <db_name>
"""

import argparse
import csv
import sys
from pathlib import Path

# Node labels and array properties share the importer's --array-delimiter.
ARRAY_DELIMITER = "|"
NAMED_THING = "biolink:NamedThing"

# Some descriptions and definitions exceed the csv module's 128 KiB default.
csv.field_size_limit(sys.maxsize)


def _property_name(column):
    return column.replace(":", "_")


def node_header(columns):
    """Map a KGX nodes.tsv header to an import header, adding ``:LABEL``."""
    special = {"id": "id:ID", "category": "category:string[]"}
    return [special.get(c, _property_name(c)) for c in columns] + [":LABEL"]


def edge_header(columns):
    """Map a KGX edges.tsv header to an import header, adding ``:TYPE``."""
    special = {"subject": ":START_ID", "object": ":END_ID"}
    return [special.get(c, _property_name(c)) for c in columns] + [":TYPE"]


def node_labels(category):
    """Return the sorted, pipe-joined labels for a node's ``category`` cell."""
    labels = set(filter(None, category.split(ARRAY_DELIMITER))) | {NAMED_THING}
    return ARRAY_DELIMITER.join(sorted(labels))


def _convert(src, dst, make_header, key_column, extra_value):
    with open(src, newline="") as fi, open(dst, "w", newline="") as fo:
        # The KGX TSV sink never quotes (it replaces tabs and newlines in values
        # with spaces), and its TSV source reads with QUOTE_NONE. Do the same,
        # or a cell that starts with '"' loses its quotes, or worse, swallows
        # the cells after it.
        reader = csv.reader(fi, delimiter="\t", quoting=csv.QUOTE_NONE)
        writer = csv.writer(fo)
        columns = next(reader)
        writer.writerow(make_header(columns))
        key = columns.index(key_column)
        for row in reader:
            writer.writerow(row + [extra_value(row[key])])


def write_nodes(src, dst):
    """Convert a KGX nodes TSV to an import nodes CSV."""
    _convert(src, dst, node_header, "category", node_labels)


def write_edges(src, dst):
    """Convert a KGX edges TSV to an import relationships CSV."""
    _convert(src, dst, edge_header, "predicate", lambda predicate: predicate)


def convert(src_dir, dst_dir, db_name):
    """Write ``nodes.csv`` and ``edges.csv`` in ``dst_dir`` for one database."""
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    write_nodes(src_dir / f"{db_name}_nodes.tsv", dst_dir / "nodes.csv")
    write_edges(src_dir / f"{db_name}_edges.tsv", dst_dir / "edges.csv")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src_dir", help="directory holding <db_name>_{nodes,edges}.tsv")
    parser.add_argument("dst_dir", help="existing directory to write nodes.csv and edges.csv")
    parser.add_argument("db_name", help="KGX basename, e.g. Cell-KN-Ontologies")
    args = parser.parse_args(argv)
    convert(args.src_dir, args.dst_dir, args.db_name)


if __name__ == "__main__":
    main()
