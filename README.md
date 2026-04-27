# NLM-CKN Extraction, Translation, and Loading

## Motivation

The National Library of Medicine (NLM) Cell Knowledge Network (NLM-CKN)
provides a comprehensive cell phenotype knowledge network that integrates
knowledge about diseases and drugs to facilitate discovery of new biomarkers
and therapeutic targets. To maximize interoperability of the derived knowledge
with knowledge about genes, pathways, diseases, and drugs from other NLM/NCBI
resources, the knowledge will be derived in the form of semantically-structured
assertions of subject-predicate-object triple statements which are compatible
with semantic web technologies, and storage using graph databases, such as the
[ArangoDB](https://arangodb.com/) database system.

The NLM-CKN captures single cell genomics data from existing data repositories,
such as CELLxGENE, and uses NSForest to identify cell type-specific marker
genes. The cell types are manually mapped to the Cell Ontology (CL), and the
marker genes are linked to data from external sources, such as Open Targets, to
provide relationships to diseases and drugs. In addition, the NLM-CKN uses
natural language processing to extract information about cell type-specific
marker genes, and their association with disease state from open access
peer-reviewed publications.

## Purpose

This repository provides:

- **A Java package** for parsing ontology OWL files, loading semantic triples
  into an ArangoDB instance, and identifying relevant subgraphs
- **Python modules** for parsing and loading ontologies, fetching data from
  external sources, and creating semantic triples from NSForest results, manual
  CL mappings, external data, and NLP results

This is a unified repository that combines the previously separate
`cell-kn-mvp-etl-ontologies` and `cell-kn-mvp-etl-results` repositories,
eliminating the need for git submodules and system-scoped JAR dependencies.

## Project Structure

```
nlm-ckn-etl/
├── pom.xml                          # Maven POM (all Java dependencies)
├── src/
│   ├── main/
│   │   ├── java/gov/nih/nlm/        # Java classes
│   │   └── shell/                   # ArangoDB shell scripts
│   └── test/
│       ├── java/gov/nih/nlm/        # Java test classes
│       └── data/
│           ├── results-2026-01-06/  # Manually curated data
│           └── results-sample/      # Sample of pipeline data
├── python/
│   ├── pyproject.toml               # Poetry configuration
│   ├── src/                         # Python modules
│   └── tests/                       # Python test files
└── docs/
    ├── java/                        # Javadoc output
    └── python/                      # Sphinx documentation
```

## Ontologies

All terms from the following ontologies have been selected for loading into the
NLM-CKN:

- [CL](http://purl.obolibrary.org/obo/cl.owl): Cell Ontology
- [GO](https://purl.obolibrary.org/obo/go/extensions/go-plus.owl): Gene Ontology
- [UBERON](http://purl.obolibrary.org/obo/uberon/uberon-base.owl): Uberon multi-species anatomy ontology
- [NCBITaxon](http://purl.obolibrary.org/obo/ncbitaxon/subsets/taxslim.owl): NCBI organismal taxonomy
- [MONDO](http://purl.obolibrary.org/obo/mondo/mondo-simple.owl): Mondo Disease Ontology
- [HP](http://purl.obolibrary.org/obo/hp.owl): Human Phenotype Ontology
- [PATO](http://purl.obolibrary.org/obo/pato.owl): Phenotype And Trait Ontology
- [HsapDv](http://purl.obolibrary.org/obo/hsapdv.owl): Human Developmental Stages

Selected terms from the following ontology have also been selected for loading:

- [PRO](http://purl.obolibrary.org/obo/pr.owl): PRotein Ontology

## External Sources

Data can be fetched from the following external sources:

- [Open Targets](https://www.opentargets.org/): Includes diseases,
  drugs, interactions, pharmacogenetics, tractability, expression, and
  depmap resources
- [Gene](https://www.ncbi.nlm.nih.gov/gene/): Records include
  nomenclature, Reference Sequences (RefSeqs), maps, pathways,
  variations, phenotypes, and links to genome-, phenotype-, and
  locus-specific resources
- [UniProt](https://www.uniprot.org/): Includes protein sequence, and
  functional information resources

## Dependencies

### Docker

Install [Docker Desktop](https://docs.docker.com/desktop/).

### ArangoDB

An ArangoDB docker image can be downloaded and a container started from the
repository root directory as follows
```
$ export ARANGO_DB_HOST=127.0.0.1
$ export ARANGO_DB_PORT=8529
$ export ARANGO_DB_HOME="<some-path>/arangodb"
$ export ARANGO_DB_APPS=$ARANGO_DB_HOME/arangodb-apps
$ export ARANGO_DB_USER=root
$ export ARANGO_DB_PASSWORD="<some-password>"
$ export ARANGO_ONTOLOGY_DB_NAME=Cell-KN-Ontologies
$ export ARANGO_PHENOTYPE_DB_NAME=Cell-KN-Phenotypes
$ export ARANGO_SCHEMA_DB_NAME=Cell-KN-Schema
$ export ARANGO_ONTOLOGY_GRAPH_NAME=KN-Ontologies-v2.0
$ export ARANGO_PHENOTYPE_GRAPH_NAME=KN-Phenotypes-v2.0
$ cd src/main/shell
$ ./start-arangodb.sh
```

### Neo4j

A Neo4j docker image can be downloaded and a container started as follows:
```
$ export NEO4J_HOME="<some-path>/neo4j"
$ export NEO4J_PASSWORD="<some-password>"
$ cd src/main/shell
$ ./start-neo4j.sh
```
The Neo4j browser is exposed at `http://localhost:7474` and the Bolt
endpoint at `bolt://localhost:7687`. Once an ArangoDB download has been
produced (see `download-arangodb.sh`), the resulting TSV files can be
loaded into Neo4j via:
```
$ ./upload-neo4j.sh
```

### Apache Jena (TDB2)

An Apache Jena Fuseki docker image (with a TDB2 backend) can be downloaded
and a container started as follows:
```
$ export JENA_HOME="<some-path>/jena"
$ export JENA_PASSWORD="<some-password>"
$ cd src/main/shell
$ ./start-jena.sh
```
The Fuseki admin UI and SPARQL endpoint are exposed at
`http://localhost:3030`. Once an ArangoDB download has been produced (see
`download-arangodb.sh`), the resulting TSV files can be transformed to
N-Triples and loaded into the Fuseki dataset via:
```
$ ./upload-jena.sh
```

### Java

Java SE 21 and Maven 3 or compatible are required to generate the Javadocs,
test, and package. From the repository root directory run:
```
$ mvn javadoc:javadoc
$ mvn test
$ mvn clean package -DskipTests
```

### Data

The Python and Java classes require the ontology files to reside in
`data/obo`. From the repository root directory you can populate this directory
as follows:
```
$ export CP="target/nlm-ckn-etl-1.0.jar"
$ java -cp $CP gov.nih.nlm.OntologyDownloader
$ java -cp $CP gov.nih.nlm.OntologySlimmer
```

The Python classes also require data in the
[cell-kn](https://github.com/NIH-NLM/cell-kn) repository to be
accessible. Clone this repository at the same level as this repository.

### Python

Python 3.12 and Poetry are required to generate the Sphinx documentation, test,
and run.

Two of the Python dependencies are fetched from GitHub over SSH, so a GitHub
SSH key must be configured before running `poetry install`. See
[Connecting to GitHub with SSH](https://docs.github.com/en/authentication/connecting-to-github-with-ssh)
if you have not set one up. One of those dependencies (`kgx`) points at a
personal fork that carries a patch not yet accepted upstream; it will be
switched back to the canonical repository once the upstream PR is merged.

From the repository root directory you can install the dependencies as
follows:
```
$ cd python
$ python3.12 -m venv .poetry
$ source .poetry/bin/activate
$ python -m pip install -r .poetry.txt
$ deactivate
$ python3.12 -m venv .venv
$ source .venv/bin/activate
$ .poetry/bin/poetry install
```
From the repository root directory generate the Sphinx documentation as
follows:
```
$ cd docs/python
$ make clean html
```
From the repository root directory run Python tests as follows:
```
$ cd python/tests
$ python -m pytest *.py
```

## Usage

### ETL Pipeline Execution Order

Each step assumes you are starting from the repository root directory.

0. **Export environment variables**

   Ensure the ArangoDB environment variables from the
   [ArangoDB](#arangodb) section above are still exported in the current
   shell, then export the additional variables below:
   ```
   $ export CP="target/nlm-ckn-etl-1.0.jar"
   $ export NCBI_EMAIL="<some-email>"
   $ export NCBI_API_KEY="<some-api-key>"
   ```

1. **Download ontologies (Java):**
   ```
   $ java -cp $CP gov.nih.nlm.OntologyDownloader
   $ java -cp $CP gov.nih.nlm.OntologySlimmer
   ```

2. **Load ontologies into ArangoDB (Java):**
   ```
   $ java -cp $CP gov.nih.nlm.OntologyGraphBuilder
   ```

3. **Fetch and transform external data (Python):**
   ```
   $ cd python/src
   $ python DataFetcher.py
   $ python DataTransformer.py
   ```

4. **Create result tuples (Python):**
   ```
   $ cd python/src
   $ python TupleWriterPipeline.py
   ```

5. **Load results into ArangoDB (Java):**
   ```
   $ java -cp $CP gov.nih.nlm.ResultsGraphBuilder
   ```

6. **Select a relevant sub-graph (Java):**
   ```
   $ java -cp $CP gov.nih.nlm.InducedSubgraphBuilder
   ```

7. **Create ArangoDB analyzers/views:**
   ```
   $ cd python/src
   $ python CellKnSchemaUtilities.py
   ```
