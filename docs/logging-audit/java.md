# Console output audit: nlm-ckn-etl/src (Java, main and test)

Audit of every console output statement under `src/main/java/gov/nih/nlm/` and
`src/test/java/gov/nih/nlm/`, done 2026-09-28 as part of the read-only audit
described in [README.md](README.md), to inform the migration to the
structured-logging approach in
`nlm-ckn-rnd/docs/proposals/logging-approach.md`. Read-only; no code changes.
Line numbers are as of `main` at `2d28ee3` and will drift as fixes land.

Scope: 35 Java files, none of them emacs lock files. 14 main files and 1 test
file contain console output. That is 117 grep hits (`System.out/err.print*`,
`printStackTrace`), plus one `System.out::println` method reference at
`OntologyGraphBuilder:476`, for 118 statements in total.

Ten classes have a `main()`: `OntologyGraphBuilder`, `ResultsGraphBuilder`,
`PhenotypeGraphBuilder`, `InducedSubgraphBuilder`, `OntologySlimmer`,
`OntologyDownloader`, `OntologyTripleParser`, `OntologyElementParser`,
`OntologyTupleWriter` and `AqlQuerySetBuilder`. `ArangoDbUtilities` also has a
demo `main()`.

Paths are relative to `src/main/java/gov/nih/nlm/` unless stated. Phase names:
F = fetch, O = ontology, R = results, A = archive.

Conventions used below:
- All timings become a `duration_ms` field, not "in X s" text.
- A stated field set is applied to all the lines in that group.
- Where an entry says MERGE, the merged line is the KEEP line named in the
  same row or group.

## 1. Per-file verdicts

### OntologyGraphBuilder.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 229 | "Constructing vertices using N triples" | MERGE into 260 |
| 260 | "Constructed N vertices using N triples in X s" | KEEP, INFO "Vertices constructed", fields records_in=triples, records_out=nVertices, duration_ms |
| 276 | "Updating vertices using…" | MERGE into 303 |
| 303 | "Updated N vertices using N triples in X s" | KEEP, INFO "Vertices updated", fields records_in, records_out, duration_ms |
| 314 | "Inserting vertices" | DROP (start banner) |
| 336 | System.err "Error inserting vertex " + doc | KEEP as ERROR. It goes to stderr, dumps the whole doc, and the exception is swallowed. |
| 343 | System.err "Error updating vertex…" | KEEP as ERROR, same problem as 336 |
| 350 | "Inserted N vertices in X s" | KEEP, INFO "Vertices inserted". nVertices counts every attempt, including deprecated skips (329-330) and failed inserts. |
| 412 | "Constructing edges using N triples" | MERGE into 477 |
| 476 | `edgeLabelFilter.summarize().forEach(System.out::println)` | KEEP as WARN "Edges dropped by label filter" |
| 477 | "Constructed N edges from N triples in X s" | KEEP, INFO "Edges constructed", fields records_in, records_out, records_rejected, duration_ms |
| 521 | "Inserting edges" | DROP |
| 543 | System.err "Error inserting edge…" | KEEP as ERROR, swallowed |
| 550 | System.err "Error updating edge…" | KEEP as ERROR, swallowed |
| 556 | "Inserted N edges in X s" | KEEP, INFO "Edges inserted". nEdges counts attempts, not successes. |

Notes on the KEEP lines:
- 336, 343, 543 and 550 should be aggregated: count failures, log the first
  few at ERROR with `collection` and `key` fields, then emit one summary with
  records_rejected. Do not pass the entire doc as the message.
- 476 is not one line. It is one line per id pair, and the message text
  embeds the id pair and the label counts, so it is not a fixed string. Use
  fixed message "Edges dropped by label filter" with fields
  `edge_collection`, `records_rejected` and `label_counts`.

### OntologySlimmer.java (phases O and A)

| Line | Current text | Verdict |
|---|---|---|
| 254 | "Wrote N classes, skipped N classes and all axioms" | KEEP, INFO "Ontology slimmed". Fields: records_out=classesWritten, records_rejected=classesSkipped, input_file, output_file, mode (inclusion or exclusion), duration_ms. |
| 511 | "Slimming pr.owl to taxa … (inclusion)" | DROP, since 254 carries the same fields |
| 514 | "Slimmed in X s" | MERGE into 254 |
| 517 | "Moving pr.owl to archive" | KEEP, INFO "Ontology file archived", phase=archive, fields from and to |
| 530 | "Building lineages for taxa … from taxslim" | DROP |
| 533 | "Lineages span N taxa across N permitted taxon(s)" | KEEP, INFO "Taxon lineages built", fields taxa_count, permitted_taxa_count |
| 534 | "Slimming uberon-base … (exclusion)" | DROP |
| 537 | "Kept N UBERON classes; slimmed in X s" | MERGE into 254 |
| 539 | "Moving uberon-base.owl to archive" | KEEP, same message as 517 |

### OntologyElementParser.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 144 | "Parsing ontology element in <file>" | DEMOTE, DEBUG per file |
| 234 | "No files found matching the pattern." | KEEP as ERROR "No ontology files found", field pattern. Today it is only a println, and the process then prints 238 and exits 0. |
| 238 | "Parsed ontology elements from N files." | KEEP, INFO "Ontology elements parsed". Fields: records_in=files, records_out=terms, duration_ms. Log only on success. |

### InducedSubgraphBuilder.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 40 | "Loading full graph via ArangoGraphLoader..." | MERGE |
| 45 | "Finding induced subgraph..." | MERGE |
| 50 | "Adding ontology hierarchy paths..." | MERGE |
| 54 | "Writing subgraph to target database..." | MERGE |

These four are phase banners with no timing. Replace them with one INFO
"Induced subgraph build finished" carrying per-step duration_ms fields, or
start and end lines for each step. The results come from the Loader, Finder
and Writer lines below.

### ResultsGraphBuilder.java (phase R)

| Line | Current text | Verdict |
|---|---|---|
| 137 | "Constructing vertices using N tuples" | MERGE into 168 |
| 168 | "Constructed N vertices using N tuples in X s" | KEEP, INFO "Vertices constructed", fields records_in, records_out, duration_ms. Add records_rejected. |
| 184 | "Updating vertices…" | MERGE into 219 |
| 219 | "Updated N vertices…" | KEEP, INFO "Vertices updated" |
| 241 | "Constructing edges…" | MERGE into 288 |
| 288 | "Constructed N edges…" | KEEP, INFO "Edges constructed" |
| 304 | "Updating edges…" | MERGE into 342 |
| 342 | "Updated N edges…" | KEEP, INFO "Edges updated" |
| 362 | "No tuples files found…" followed by `System.exit(1)` | KEEP as ERROR "No tuples files found", fields pattern and dir. The exit is invisible to a JSON consumer. |
| 403 | "Processing tuples file <path>" | KEEP as INFO "Tuples file processed". Move it to the end of each file with `tuples_file`, records_in, records_rejected, duration_ms. |

Lines 168 to 342 fire once per input file. That is 8 lines per file, or 4
after merging. Prefer one summary line per file.

### OntologyTripleParser.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 124 | "Collecting triples from within <file>" | MERGE into 183 |
| 135 | "Filter on namespaces [...]" | DEMOTE, DEBUG. This is a set as message text; put it in a field. |
| 183 | "Collected triples from within <file> in X s" | KEEP, INFO "Triples collected from ontology file". Fields: file, records_in (statements seen), records_out (triples), records_rejected, duration_ms. Today it has no count at all. |
| 272 | `"=== file ==="` banner | DEMOTE. This is the `--report-imports` diagnostic mode, so stdout is arguably its purpose; leave it as plain output or use DEBUG. |
| 276 | "  rootNS: …" | MERGE into one line per file |
| 279 | "  imports: (none)" | MERGE |
| 282 | "  import: …" per import | MERGE |
| 287 | "  class namespaces: N" | MERGE |
| 290 | "  class: … (root)" per namespace | MERGE |
| 293 | "  ERROR: " + msg | KEEP as ERROR. It goes to stdout and swallows the exception, and the loop continues with no file or stack. |
| 295 | blank println | DROP |
| 308 | "Collecting unique triples from within N files" | MERGE into 316 |
| 316 | "Collected N unique triples from within N in X s" | KEEP, INFO "Unique triples collected". Fields: records_in (sum of file triples), records_out (unique), files, duration_ms. |

The report-mode lines (272 to 295) can be one INFO per file with fields
root_ns, imports, class_namespaces.

### OntologyDownloader.java (phase F, with A for the archive step)

| Line | Current text | Verdict |
|---|---|---|
| 47 | "Parsing <file>" | DEMOTE. It is called two or three times per URL. |
| 70 | "Could not get version for <file>" | KEEP as WARN "Ontology version not found", field file. It has real consequences (see gaps). |
| 88 | "Getting <url>" | MERGE into the per-URL summary |
| 103 | "Writing <newFile>" | DROP |
| 107 | "Found new version" | MERGE into the summary, field version_new |
| 112 | "Found current version" | MERGE into the summary, field version_cur |
| 119 | "Renaming cur to archive/old" | KEEP, INFO "Ontology updated", phase=archive. Fields: ontology, version_prev, version_new, archived_to. |
| 122 | "Renaming new to cur" | MERGE into 119 |
| 125 | "New version is not newer…" | KEEP, INFO "Ontology unchanged", fields ontology, version_cur, version_new |
| 126 | "Removing <newFile>" | DROP |
| 130 | "Renaming new to cur" (first download) | KEEP, INFO "Ontology downloaded", fields ontology, version_new |

Every branch should also carry url, http_status, bytes and duration_ms.

**Update (PR #107):** the null-version delete and the abort-on-first-failed-URL
behavior this table describes were fixed ahead of the logging work. The method
now takes an `HttpClient` parameter and the per-URL decision moved into a
package-private `installDownload`, which changes some of the line numbers and
branches above but not the logging-verdict shape.

### InducedSubgraphFinder.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 57 | "Source vertices: N" | MERGE into 251 |
| 62 | "Reachable vertices: N" | MERGE into 251 |
| 88 | "Skipping hierarchy paths for X (strategy)" | DEMOTE. It is per prefix. |
| 90 | "Adding hierarchy paths for X (strategy)" | DEMOTE |
| 128 | "After hierarchy enrichment:" | MERGE |
| 129 | "  Vertices: N" | KEEP as one INFO "Hierarchy enrichment finished". Fields: vertices, edges, vertices_added, edges_added, duration_ms. |
| 130 | "  Edges: N" | MERGE into 129 |
| 251 | "Induced edges: N" | KEEP as INFO "Induced subgraph found". Fields: records_in=graph vertices, source_vertices, reachable_vertices, records_out=induced edges, max_depth, source_collection, duration_ms. |

### ArangoGraphLoader.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 109 | "Full graph vertices: N" | KEEP, INFO "Graph loaded". Fields: graph, records_out vertices, edges, records_rejected (dangling edges), duration_ms. |
| 110 | "Full graph edges: N" | MERGE into 109 |

### ArangoGraphWriter.java (phase R, or O)

| Line | Current text | Verdict |
|---|---|---|
| 51 | "Creating collections..." | DROP |
| 62 | "Inserting vertices..." | DEMOTE. It marks progress in a long loop. |
| 70 | "Inserting edges..." | DEMOTE |
| 80 | "Registering named graph..." | DROP |
| 93 | "Named graph 'x' created in database 'y'." | KEEP, INFO "Subgraph written". Fields: graph, database, vertices_written, edges_written, vertex_collection_count, edge_collection_count, duration_ms. |
| 94 | "  Vertex collections: [list]" | MERGE into 93 |
| 95 | "  Edge collections: [list]" | MERGE into 93 |

### ArangoDbUtilities.java (phases O and R)

| Line | Current text | Verdict |
|---|---|---|
| 75 | "Creating database: X" | KEEP, INFO "Database created", field database |
| 81 | "Getting database: X" | DROP |
| 93 | "Deleting database: X" | KEEP as WARN "Database dropped". It is destructive and runs every run. |
| 110 | "Creating graph: X" | KEEP, INFO "Graph created" |
| 115 | "Getting graph: X" | DROP |
| 128 | "Deleting graph: X" | KEEP as WARN "Graph dropped" |
| 143 | "Creating vertex collection" | DEMOTE. It fires about 20 times per run. |
| 147 | "Getting vertex collection" | DROP |
| 160 | "Deleting vertex collection" | DEMOTE |
| 181 | "Creating edge collection" | DEMOTE. It fires about 50 or more times. |
| 186 | "Getting edge collection" | DROP |
| 199 | "Deleting edge collection" | DEMOTE |
| 232 | "Vertex collections:" | DROP |
| 238 | printf per vertex collection | KEEP, INFO "Collection document count", fields collection, kind, records_out |
| 240 | printf "TOTAL" | KEEP, INFO "Database summary", fields vertex_total, edge_total |
| 244 | "Edge collections:" | DROP |
| 249 | printf per edge collection | KEEP, same message as 238 |
| 251 | printf "TOTAL" | MERGE into 240 |

The `printSummary` method uses printf column formatting, which is
unparseable as structured logging. It is also the only post-load
reconciliation. No callers of `printSummary` were found in the Java sources
read; confirm it is invoked from somewhere or wire it into the end of each
builder's `main()`. (Later confirmed: no callers found. See the top-10 gap
list below and PR #108's notes, which wires the `records_out` read-back into
the insert-summary work instead of converting `printSummary` itself.)

### PhenotypeGraphBuilder.java (deprecated; phase O)

| Line | Current text | Verdict |
|---|---|---|
| 237 | Full AQL text | DEMOTE. It is a high-cardinality value; use a `query_id` field at INFO and the text at DEBUG. |
| 238 | bindVars map | DEMOTE |
| 247 | "Collected N paths in X s" | KEEP, INFO "AQL paths collected". Fields: query_id, records_out, duration_ms. It is not identifiable today. |
| 259 | "Collecting unique vertex documents from N paths" | MERGE into 273 |
| 273 | "Collected N unique vertex documents …" | KEEP, INFO "Vertex documents collected", fields records_in=paths, records_out, duration_ms |
| 284 | "Collecting unique edge documents…" | MERGE into 298 |
| 298 | "Collected N unique edge documents…" | KEEP, INFO "Edge documents collected" |
| 312 | "Inserting N vertex documents" | MERGE into 337 |
| 337 | "Inserted N vertex documents in X s" | KEEP, INFO "Vertex documents inserted". The N is list size, not actual inserts. Split into inserted and replaced counts. |
| 347 | "Inserting N edge documents" | MERGE into 365 |
| 365 | "Inserted N edge documents in X s" | KEEP, INFO "Edge documents inserted", same caveat as 337 |

`PhenotypeGraphBuilder` is `@Deprecated`; per project decision, its logging
work is skipped in favor of `InducedSubgraphBuilder`. Kept here for the
record.

### OntologyTupleWriter.java (phase O)

| Line | Current text | Verdict |
|---|---|---|
| 95 | "Skipped N triples containing a blank node" | KEEP as WARN "Triples skipped", field records_rejected |
| 97 | "Wrote N tuples to <path>" | KEEP, INFO "Tuples written". Fields: records_out, output_path, records_rejected, duration_ms. |
| 132 | "Collecting triples from [list of paths]" | MERGE into 97. The list is a field, not the message: input_files and pattern. |

### AqlQuerySetBuilder.java (dev harness `main`, lines 476-564)

| Line | Current text | Verdict |
|---|---|---|
| 552 | Query text println | DEMOTE, DEBUG |
| 560 | "Collected N paths in X s" | KEEP, INFO with query_id, records_out, duration_ms |

### src/test/java/gov/nih/nlm/ArangoDbUtilitiesTest.java

| Line | Current text | Verdict |
|---|---|---|
| 56 | `e.printStackTrace()` in `@BeforeEach` | DROP. Swallows a failed ArangoDB start or stop and the test continues. Rethrow as RuntimeException or use `fail()`. |
| 80 | `e.printStackTrace()` in `@AfterEach` | DROP, same fix |

There are no other prints in tests.

### Exception swallowing summary

- Stdout error swallowed: OntologyTripleParser:293.
- Stderr error swallowed, with no aggregate:
  - OntologyGraphBuilder:336, 343, 543, 550.
  - Inserts are best-effort and the load continues. Because the counter
    (350, 556) still counts the failed docs, the summary can claim success.
    **Fixed in PR #108**, which counts inserted/updated/skipped/failed
    separately and raises after attempting every document.
- printStackTrace swallowed: the two test lines above
  (ArangoDbUtilitiesTest:56, 80).
- Failure reported only as println with a system exit or a normal exit:
  - ResultsGraphBuilder:362.
  - OntologyElementParser:234 followed by 238. **Fixed in PR #106**
    (`requireFiles` now throws `IllegalStateException`).
- No main-level `catch` writes a log line before rethrowing:
  - Every main rethrows via `new RuntimeException(e)`, or lets exceptions
    propagate.
  - The only failure record is the JVM stack trace on stderr.

## 2. GAPS: messages that should exist but don't

**Top-level start/finish/failed lines** are missing in all ten `main()`
methods:
- OntologyGraphBuilder:564
- ResultsGraphBuilder:350
- PhenotypeGraphBuilder:373
- InducedSubgraphBuilder:59
- OntologySlimmer:501
- OntologyDownloader:141
- OntologyTripleParser:326
- OntologyElementParser:224
- OntologyTupleWriter:108
- AqlQuerySetBuilder:476

Each needs INFO "Run started", INFO "Run finished" (with `duration_ms`,
counts and `outcome`), and ERROR "Run failed" (with the exception). Also log
the effective config: CKN_RUN and TUPLES_DIR (ResultsGraphBuilder:42-44),
USR_DIR and OBO_DIR (PathUtilities), and args.

**Silent record drops, the data-loss risks** (need records_rejected counts,
with a reason field):
- ResultsGraphBuilder:
  - 142, 148, 189, 202, 246, 250, 254 and 258: tuples are skipped because
    they are not triples, or the subject or object is not a valid vertex, or
    the predicate label is null. Skips at 309 to 317 are the same for
    quintuples.
  - Tuples that are neither 3 nor 5 elements (lines 105-115) are never
    validated and never counted.
  - Line 98 `value.contains("http")` silently classifies any literal
    containing "http" as a URI.
  - Line 285 overwrites the edge Label on collision ("always assign the last
    label"), so one edge silently loses all but the last label.
    OntologyGraphBuilder:472 appends instead.
  - Lines 208-209 and 331-332 throw on a missing vertex or edge, with a
    message that lacks the tuples file and index.
- OntologyGraphBuilder:
  - 78-98: `createVTuple` returns "invalid" for a non-URI node, a URI parse
    `RuntimeException` (line 82, silent catch), a null path or no
    underscore.
  - 420 and 424: an invalid subject or object drops the edge with no count.
    This is the biggest one, including non-permitted NCBITaxon (113-114).
  - 428: null label. 433: taxon-constraint predicates. 440: label filter
    (only partly summarised at 476).
  - 293: null attribute or literal.
  - 324-331: deprecated or obsolete terms are written to
    `deprecated_terms.txt` and skipped, with no count and no path logged.
  - 537-545: an edge is not inserted when an endpoint vertex is missing (for
    example a deprecated one), with no else, no log and no count. nEdges
    still counts it. **Fixed in PR #108**: now counted as `skipped`, not
    silently dropped from the count.
- OntologyTripleParser:
  - 139, 146, 150 and 175: classes and statements filtered by namespace or
    predicate.
  - 154: statements with an anonymous object and a non-subClassOf predicate
    are dropped.
  - 173: restrictions missing a predicate or object are dropped.
  - 311 and 271: `ro.owl` is skipped silently.
  - 313: duplicate triples are collapsed, so there is no records_in versus
    unique count.
- ArangoGraphLoader:97: edges with endpoints outside the graph are skipped
  silently.
- OntologySlimmer: 221 to 229 (records kept and skipped are counted, but
  only in `filterClasses`). Lines 197-199 and 231-234 drop all `owl:Axiom`
  silently, with no axiom count.
- OntologyTupleWriter:74-77: blank-node skips are counted and logged. This
  is the one OK example.
- OntologyElementParser:
  - 107-112: terms with no underscore, or with id "valid", are skipped.
  - 117-120: duplicate term labels overwrite silently.
  - 189: enrichment is skipped silently when "ro" is missing.
- PhenotypeGraphBuilder:245: a query returning zero paths is not flagged. A
  silently empty phenotype subgraph is plausible.
- InducedSubgraphFinder:
  - 50-57: zero source vertices gives an empty subgraph with no WARN.
  - 215 and 223: vertices excluded by IGNORED_VERTEX_COLLECTIONS or
    IGNORED_EDGE_COLLECTIONS are not counted.

**File and DB operations without result counts or paths:**
- OntologyGraphBuilder:610-614: `edge_labels.txt` is written with no path or
  count logged. `deprecated_terms.txt` (317) has none either.
- OntologyGraphBuilder:659, ResultsGraphBuilder:437 and
  PhenotypeGraphBuilder:400: `arangoDB.shutdown()` with no log.
- InducedSubgraphBuilder:81-90: drops and recreates the target database and
  graph with no log. Line 84 ignores the `createDatabase` boolean.
- ArangoDbUtilities:129: the `graph.drop()` result is unchecked and
  unlogged. Lines 163 and 202 are the same for `remove`.
- OntologyGraphBuilder:582, 584, 631 and 633 are destructive `deleteDatabase`
  and `deleteGraph` calls (they log only at the utility level).
- ArangoGraphWriter:
  - 53 and 58: `createCollection` with no count.
  - 66 and 76: single-document inserts with no counts. A failure part-way
    leaves a partial DB and nothing records how far it got.
- ArangoGraphLoader:60-107: per-collection load counts are not logged.
- PhenotypeGraphBuilder:321: falls back silently to the path document when
  the ontology-graph vertex is missing.

**Phase start/end with no log:**
- OntologyGraphBuilder main: "ontology" (lines 575-607) and "phenotype"
  (624-656) sections have no phase boundary. Both use the same helper names,
  so the phase is not recoverable from logs.
- ResultsGraphBuilder main: all of it.
- OntologySlimmer main: pr slim (504-518), uberon slim (521-540).
- OntologyDownloader:83-133: the per-URL loop has no phase boundary or
  summary.
- ArangoDbUtilities constructors (29-41): the Arango connection target is
  not logged (host and port only, never the password). Missing env vars
  throw NPE or NumberFormatException with no message.

**Long operations without `duration_ms`:**
- OntologyGraphBuilder:659 (shutdown), 582 and 585 (DB and graph setup).
- OntologyElementParser:140-178 (DOM parse of large OWL files, no timing at
  all).
- ArangoGraphLoader:37 (full graph load).
- ArangoGraphWriter:38 (whole subgraph write).
- OntologyDownloader:97 (HTTP download) and 104.
- OntologySlimmer:414-455 (`readChildToParents`).
- OntologyTripleParser:128-129 (model read, timed only as part of the whole
  per-file collection at 183).

**Resolved input versions and paths not logged:**
- OntologyElementParser:160-165 parses `versionIRI`, PURL, title and root
  into `OntologyElementMap`, but never logs them. A run cannot be tied to
  the ontology versions it used.
- OntologyDownloader logs versions at 107 and 112 but not for files that
  already exist and are skipped, and 70 warns without file path context.
- OntologyGraphBuilder:567-571 and 617-621: the resolved list of `.owl`
  files (the glob result) is never logged. The same applies at
  ResultsGraphBuilder:353-364 and 367-379, and OntologyTripleParser:329-338.
- OntologySlimmer:502-529: no log of the input file versions or of the
  slimmed output file.
- OntologyDownloader:97-99: the HTTP response has no redirect-final `uri()`,
  `statusCode`, content-length or last-modified logged.
- OntologyDownloader:114: when either version is null, the new download is
  silently treated as "not newer" and deleted at 126 to 127, so the stale
  file is kept. **Fixed in PR #107.**

**Error paths with no context:**
- OntologyDownloader:99: on a non-200, an IOException aborts the whole loop,
  so remaining ontologies are neither fetched nor reported. Which URL failed
  and how many succeeded is not logged. **Fixed in PR #107**: every URL is
  now attempted and all failures are reported together.
- OntologyElementParser:210: "Could not put RO terms" drops the cause and
  the term.
- ArangoGraphLoader:75 and 103: a generic "Failed to deserialize
  vertex/edge" gives no `_id`, collection or doc excerpt.
- ResultsGraphBuilder:83 and 89: the exception messages do carry the file
  path. This is one good example.
- OntologySlimmer:517 and 539: `Files.move ... REPLACE_EXISTING` archives
  the input even when the slim produced 0 classes, with no check or WARN.
  Lines 508-529 also silently overwrite any existing archived file.
- OntologySlimmer:531-533: if a permitted taxon is not present in taxslim,
  `walkLineage` returns just the taxon's own URI. This silently
  over-excludes, and the lineage sizes are not checked per taxon.

## 3. Summary

Totals across the 118 statements:

| Verdict | Count |
|---|---|
| KEEP | 48 |
| MERGE | 37 |
| DROP | 18 (including the 2 test `printStackTrace` calls, which should throw instead) |
| DEMOTE | 15 |

Error handling: about 11 lines log an error or warning-worthy condition to
stdout or stderr, or swallow it:
- OntologyTripleParser:293 (stdout)
- OntologyGraphBuilder:336, 343, 543, 550 (stderr)
- ResultsGraphBuilder:362 (stdout then exit)
- OntologyElementParser:234 (stdout, then exit 0)
- OntologyDownloader:70 (stdout, should be WARN)
- OntologyTupleWriter:95 (stdout, should be WARN)
- The two test lines above.

### Top 10 highest-value gaps

1. **No rejected-record counts anywhere in the graph-building path.**
   ResultsGraphBuilder:142-258 and 309-317, and OntologyGraphBuilder:78-98,
   420, 424, 428, 433, 440 and 537-545. This includes edges silently not
   inserted because an endpoint vertex is missing. Rejected counts are what
   catch silent data loss.
2. **Failed vertex and edge inserts and updates are swallowed.**
   OntologyGraphBuilder:336, 343, 543 and 550. The load continues and the
   "Inserted N" summary at 350 and 556 counts attempts, not successes. Log
   aggregated failures with records_rejected and mark the run as failed or
   degraded. **Fixed in PR #108.**
3. **No run-level start, finish and failed lines in any `main()`** (ten
   places listed above), with no outcome and no config or paths logged.
   Also every main rethrows through `new RuntimeException(e)` with no log
   line.
4. **Ontology input versions and resolved file lists not logged.**
   OntologyElementParser:160-165, OntologyGraphBuilder:567-571, plus
   OntologySlimmer main and the downloader's skipped or unchanged files. A
   run cannot be tied to the OWL versions it loaded.
5. **Downloader has no per-ontology outcome and mishandles a null version.**
   OntologyDownloader:98-133 has no failed-URL log, and a null version at
   114 makes the new download look "not newer" and deletes it. Add one INFO
   per URL with action, http_status, bytes and duration. **Fixed in PR
   #107** (both parts of this gap).
6. **Empty or degenerate outputs are not flagged.** OntologySlimmer:517 and
   539 archive the input even when 0 classes are kept, and 531-533 can
   silently produce an over-exclusive lineage. PhenotypeGraphBuilder:245 has
   zero-path queries. InducedSubgraphFinder:50-57 can have zero source
   vertices.
7. **ArangoGraphLoader:97 drops dangling edges silently, and
   ArangoGraphLoader:37 has no duration_ms.** Log records_in and rejected
   per collection plus the total load time. The generic deserialize errors
   at 75 and 103 lack `_id` and collection.
8. **ArangoGraphWriter (53-91) does per-document inserts with no counts, no
   timing and no partial-failure reporting.** A failure part-way leaves a
   partial phenotype DB. This is the largest unobserved write.
9. **Destructive DB and graph drops and non-logged shutdown.**
   InducedSubgraphBuilder:81-90 drops and recreates the target database with
   no log. Its env defaults (66-69) silently allow empty DB and graph names.
   OntologyGraphBuilder:582-585 and 631-634, and the `shutdown()` calls, are
   only partly covered by the ArangoDbUtilities lines.
10. **ResultsGraphBuilder:362 and OntologyElementParser:234 report failure
    as plain println.** The `System.exit(1)` at 362 is invisible to a JSON
    consumer, and 234 exits 0 after printing "Parsed … 0 files". Both need
    ERROR lines. **OntologyElementParser:234 fixed in PR #106**;
    ResultsGraphBuilder:362 already fails (exits 1) and is left for the
    logging work to add the ERROR line.

Other notable items: ResultsGraphBuilder:285 overwrites edge labels where
OntologyGraphBuilder:472 appends. The `printSummary` collection counts
(ArangoDbUtilities:214-257) are the only post-load reconciliation but appear
uncalled from Java code. PhenotypeGraphBuilder is `@Deprecated`, so its
logging work is skipped in favour of InducedSubgraphBuilder.
