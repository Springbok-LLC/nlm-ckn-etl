"""Convert one database's arangodump to ``neo4j-admin database import`` CSV.

Reads the dump directory of one database, e.g. ``Cell-KN-Ontologies/`` from
``runs/{run}/06-golden-dump.tar.gz``, and writes into ``<dst_dir>``:

- ``nodes/<collection>.csv`` per document collection and
  ``relationships/<collection>.csv`` per edge collection, each with its own
  header
- ``import.args``: the importer options, one per line, for
  ``neo4j-admin database import full neo4j @<dst_dir>/import.args``
- ``counts.env``: ``NODES=<n>`` and ``RELATIONSHIPS=<n>``, the dump's totals,
  so the import can be checked against them

Building from the dump rather than the KGX export keeps Neo4j identical to an
ArangoDB restored from the same dump: every edge document is written, so
parallel edges that differ only by ``Label`` survive, node ids come from
``_id`` rather than from ``id`` annotations, and lists stay lists.

The graph model:

- node id: ``_id`` with its first ``/`` replaced by ``:`` (``HP/0012871`` ->
  ``HP:0012871``), stored as the ``id`` property. A document's own ``id``
  attribute (copied from ``oboInOwl:id``, and sometimes stale) is renamed
  ``oboInOwl_id``.
- node labels: the collection name and ``biolink:NamedThing``, also stored
  as ``category`` (``string[]``); ``provided_by`` is ``infores:nlm-ckn``.
- relationships: one per edge document, typed by ``relationship_type``,
  with ``id`` set to the edge's ``_id`` and Biolink provenance derived from
  ``Source`` as ``flows.pipeline.export_kgx`` derives it.
- properties: every other field, with ``:`` in its name replaced by ``_`` (a
  ``:`` in an import header is a type suffix). ``_key``, ``_rev``, ``_id``,
  ``_from`` and ``_to`` are dropped.

Values are otherwise verbatim. Every value is quoted, so an empty string stays
an empty string while a field a document lacks is left unquoted and empty,
which the importer treats as absent.

Deliberately dependency-free: src/main/docker/neo4j/Dockerfile runs it in a
bare python image.

Usage: python ArangoDumpToNeo4jImport.py <dump_dir> <dst_dir>
"""

import argparse
import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path

from InfoResUtilities import NLM_CKN_INFORES, source_to_infores

# Choices agreed for the dataset image; change them here.
#
# A field that is a list in any document of a collection becomes a string[]
# property, with that field's scalar values wrapped as one-element lists.
# False joins lists into plain strings with LIST_JOINER instead, as KGX did.
ARRAY_FIELDS = True
LIST_JOINER = "|"
# Values keep their newlines, and the importer is told to expect them. False
# replaces each line break with a space instead, as KGX did.
KEEP_NEWLINES = True


def relationship_type(collection, edge):
    """Return the relationship type for an edge document: its collection, e.g. ``CL-CL``.

    The semantic predicate stays in the ``Label`` property.
    """
    return collection


# Separates labels and array elements. Not '|', which occurs inside list
# elements.
ARRAY_DELIMITER = "\x1f"
NAMED_THING = "biolink:NamedThing"

# structure.json parameters.type
DOCUMENT_COLLECTION = 2
EDGE_COLLECTION = 3

DROPPED_FIELDS = {"_key", "_rev", "_id", "_from", "_to"}
RENAMED_NODE_FIELDS = {"id": "oboInOwl_id"}

NODE_HEADER = ["id:ID", ":LABEL", "category:string[]", "provided_by"]
EDGE_HEADER = [
    ":START_ID",
    ":END_ID",
    ":TYPE",
    "id",
    "aggregator_knowledge_source",
    "primary_knowledge_source",
    "knowledge_source",
    "supporting_data_source:string[]",
]

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class Collection:
    name: str
    is_edge: bool
    data: Path


def node_id(arango_id):
    """Map an ArangoDB ``_id`` to a node id: ``HP/0012871`` -> ``HP:0012871``."""
    return arango_id.replace("/", ":", 1)


def edge_provenance(source):
    """Return an edge's Biolink provenance slots from its ``Source`` (a string or list).

    As ``export_kgx`` sets them: ``infores:nlm-ckn`` aggregated every edge;
    the first CURIE is both the primary and the knowledge source and the rest
    are supporting sources; with no CURIE, the knowledge source is
    ``infores:nlm-ckn`` and there is no primary source.
    """
    names = source if isinstance(source, list) else [source] if source else []
    ids = list(dict.fromkeys(filter(None, map(source_to_infores, names))))
    return {
        "aggregator_knowledge_source": NLM_CKN_INFORES,
        "primary_knowledge_source": ids[0] if ids else None,
        "knowledge_source": ids[0] if ids else NLM_CKN_INFORES,
        "supporting_data_source": ids[1:] or None,
    }


def read_dump(dump_dir):
    """Return the collections of one database's arangodump directory, sorted by name.

    Only the plain format is supported: ``dump.json`` must say
    ``useEnvelope: false`` and ``useVPack: false``, and every collection must
    have exactly one ``<name>_<hash>.data.json.gz`` beside its
    ``.structure.json``. System collections are skipped.
    """
    dump_dir = Path(dump_dir)
    meta = json.loads((dump_dir / "dump.json").read_text())
    for flag in ("useEnvelope", "useVPack"):
        if meta.get(flag) is not False:
            raise ValueError(f"{dump_dir}/dump.json: {flag} is {meta.get(flag)!r}; only false is supported")
    collections, expected = [], set()
    for structure in dump_dir.glob("*.structure.json"):
        params = json.loads(structure.read_text())["parameters"]
        data = structure.with_name(structure.name.removesuffix(".structure.json") + ".data.json.gz")
        expected.add(data.name)
        if params["name"].startswith("_"):
            continue
        if params["type"] not in (DOCUMENT_COLLECTION, EDGE_COLLECTION):
            raise ValueError(f"{structure}: unknown collection type {params['type']!r}")
        collections.append(Collection(params["name"], params["type"] == EDGE_COLLECTION, data))
    found = {p.name for p in dump_dir.glob("*.data.json*")}
    if found != expected:
        raise ValueError(
            f"{dump_dir}: expected one .data.json.gz per collection; "
            f"missing {sorted(expected - found)}, unexpected {sorted(found - expected)}"
        )
    if not collections:
        raise ValueError(f"{dump_dir}: no collections")
    return sorted(collections, key=lambda c: c.name)


def _documents(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _property_columns(docs, reserved, renamed):
    """Map each field of a collection's documents to its import header column."""
    fields, lists = set(), set()
    for doc in docs:
        fields.update(doc)
        lists.update(field for field, value in doc.items() if isinstance(value, list))
    columns, taken = {}, set(reserved)
    for field in sorted(fields - DROPPED_FIELDS, key=lambda f: renamed.get(f, f)):
        name = renamed.get(field, field).replace(":", "_")
        if name in taken:
            raise ValueError(f"field {field!r} would be stored as {name!r}, which is already taken")
        taken.add(name)
        columns[field] = name + ":string[]" if ARRAY_FIELDS and field in lists else name
    return columns


def _string(value):
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__} {value!r}")
    return value if KEEP_NEWLINES else _LINE_BREAK.sub(" ", value)


def _array(values):
    values = [_string(v) for v in values]
    # The importer reads an empty string[] cell as absent, not as [] or [""].
    if not values or "" in values:
        raise ValueError(f"cannot import an empty list or empty list element: {values!r}")
    if any(ARRAY_DELIMITER in v for v in values):
        raise ValueError(f"list element contains the array delimiter U+{ord(ARRAY_DELIMITER):04X}")
    return ARRAY_DELIMITER.join(values)


def _cell(value, column):
    if column.endswith("[]"):
        return _array(value if isinstance(value, list) else [value])
    if isinstance(value, list):
        return LIST_JOINER.join(map(_string, value))
    return _string(value)


def _csv_line(cells):
    """Quote every cell; None, an absent value, stays an unquoted empty field."""
    return ",".join("" if c is None else '"' + c.replace('"', '""') + '"' for c in cells) + "\n"


def _rows(collection, docs, columns):
    labels = ARRAY_DELIMITER.join([collection.name, NAMED_THING])
    for doc in docs:
        try:
            if collection.is_edge:
                slots = edge_provenance(doc.get("Source"))
                supporting = slots["supporting_data_source"]
                row = [
                    node_id(doc["_from"]),
                    node_id(doc["_to"]),
                    relationship_type(collection.name, doc),
                    doc["_id"],
                    slots["aggregator_knowledge_source"],
                    slots["primary_knowledge_source"],
                    slots["knowledge_source"],
                    supporting and _array(supporting),
                ]
            else:
                row = [node_id(doc["_id"]), labels, labels, NLM_CKN_INFORES]
            row += [_cell(doc[f], c) if f in doc else None for f, c in columns.items()]
        except (KeyError, ValueError) as e:
            raise ValueError(f"{collection.name} document {doc.get('_id')}: {e}") from None
        yield row


def write_collection(collection, path):
    """Write one collection's import CSV to ``path`` and return its row count."""
    docs = _documents(collection.data)
    header = EDGE_HEADER if collection.is_edge else NODE_HEADER
    renamed = {} if collection.is_edge else RENAMED_NODE_FIELDS
    reserved = {column.split(":")[0] for column in header} - {""}
    try:
        columns = _property_columns(docs, reserved, renamed)
    except ValueError as e:
        raise ValueError(f"{collection.name}: {e}") from None
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(_csv_line(header + list(columns.values())))
        for row in _rows(collection, docs, columns):
            f.write(_csv_line(row))
    return len(docs)


def convert(dump_dir, dst_dir):
    """Write the import CSVs, ``import.args`` and ``counts.env`` into ``dst_dir``.

    Returns the number of nodes and relationships written.
    """
    dst_dir = Path(dst_dir).resolve()
    args = [f"--array-delimiter=U+{ord(ARRAY_DELIMITER):04X}"]
    if KEEP_NEWLINES:
        args.append("--multiline-fields=true")
    totals = {"nodes": 0, "relationships": 0}
    for collection in read_dump(dump_dir):
        kind = "relationships" if collection.is_edge else "nodes"
        path = dst_dir / kind / f"{collection.name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        totals[kind] += write_collection(collection, path)
        args.append(f"--{kind}={path}")
    (dst_dir / "import.args").write_text("".join(arg + "\n" for arg in args))
    (dst_dir / "counts.env").write_text(
        f"NODES={totals['nodes']}\nRELATIONSHIPS={totals['relationships']}\n"
    )
    return totals["nodes"], totals["relationships"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dump_dir", help="one database's dump directory, holding dump.json")
    parser.add_argument("dst_dir", help="directory to write the import files into")
    args = parser.parse_args(argv)
    nodes, relationships = convert(args.dump_dir, args.dst_dir)
    print(f"Wrote {nodes} nodes and {relationships} relationships to {args.dst_dir}")


if __name__ == "__main__":
    main()
