from collections import deque
import os

import networkx as nx
import nx_arangodb as nxadb
from arango import ArangoClient


def descendants_at_depth(G, source, max_depth):
    """BFS traversal from source up to max_depth."""
    visited = {source}
    queue = deque([(source, 0)])

    while queue:
        node, depth = queue.popleft()
        if depth < max_depth:
            for neighbor in G.successors(node):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))

    return visited


# Replace the reachability section in build_induced_subgraph
MAX_DEPTH = 5  # adjust to your domain knowledge


def build_induced_subgraph(
    db, graph_name: str, source_collection: str, target_db, subgraph_name: str
) -> None:
    """
    Build a named graph in target_db containing the induced subgraph of all
    vertices reachable from any vertex in source_collection, preserving multiple
    vertex and edge collections.
    """

    # Step 1 — Load the full graph via the NetworkX adapter
    G_arango = nxadb.MultiDiGraph(name=graph_name, db=db)
    G = nx.MultiDiGraph(G_arango)

    # Step 2 — Find all vertices reachable from any source vertex
    source_vertices = {v for v in G.nodes if v.startswith(f"{source_collection}/")}
    print(f"Source vertices: {len(source_vertices)}")

    reachable = set()
    for source in source_vertices:
        print(f"Source: {source}")
        reachable.update(descendants_at_depth(G, source, MAX_DEPTH))

    # Include source vertices themselves
    reachable.update(source_vertices)

    # Step 3 — Extract induced subgraph
    induced = G.subgraph(reachable).copy()

    print(f"Reachable vertices: {induced.number_of_nodes()}")
    print(f"Induced edges:      {induced.number_of_edges()}")

    # Step 4 — Identify which original vertex and edge collections are represented
    # Vertex and edge IDs are of the form "collectionName/key"
    original_collections = {v.split("/")[0] for v in induced.nodes}
    original_edge_collections = {
        data["_id"].split("/")[0]
        for _, _, data in induced.edges(data=True)
        if "_id" in data
    }

    # Step 5 — Collection names are unchanged since the subgraph is in its own database
    vertex_col_map = {col: col for col in original_collections}
    edge_col_map = {col: col for col in original_edge_collections}

    # Step 6 — Drop and recreate all new collections in target_db. Note that we
    # need to drop the graph first, so that the collections can be dropped.
    if target_db.has_graph(subgraph_name):
        target_db.delete_graph(subgraph_name)

    for col in vertex_col_map.values():
        if target_db.has_collection(col):
            target_db.delete_collection(col)
        target_db.create_collection(col)

    for col in edge_col_map.values():
        if target_db.has_collection(col):
            target_db.delete_collection(col)
        target_db.create_collection(col, edge=True)

    # Step 7 — Insert vertices into their respective collections in target_db
    for node, data in induced.nodes(data=True):
        old_col, key = node.split("/", 1)
        col = vertex_col_map[old_col]
        if not target_db.collection(col).has(key):
            target_db.collection(col).insert({**data, "_key": key})

    # Step 8 — Insert edges into their respective collections in target_db,
    # rewriting _from/_to to use the (unchanged) collection names
    for u, v, data in induced.edges(data=True):
        from_col, from_key = u.split("/", 1)
        to_col, to_key = v.split("/", 1)

        new_from = f"{vertex_col_map[from_col]}/{from_key}"
        new_to = f"{vertex_col_map[to_col]}/{to_key}"

        orig_edge_col = data.get("_id", "").split("/")[0]
        edge_col = edge_col_map.get(orig_edge_col)
        if edge_col is None:
            print(
                f"Warning: edge {data.get('_key')} has no recognized collection, skipping."
            )
            continue

        if not target_db.collection(edge_col).has(data["_key"]):
            target_db.collection(edge_col).insert(
                {**data, "_from": new_from, "_to": new_to}
            )

    # Step 9 — Register as a named graph in target_db
    vertex_collections = list(vertex_col_map.values())

    target_db.create_graph(
        subgraph_name,
        edge_definitions=[
            {
                "edge_collection": col,
                "from_vertex_collections": vertex_collections,
                "to_vertex_collections": vertex_collections,
            }
            for col in edge_col_map.values()
        ],
    )

    print(f"Named graph '{subgraph_name}' created in database '{target_db.name}'.")
    print(f"  Vertex collections: {vertex_collections}")
    print(f"  Edge collections:   {list(edge_col_map.values())}")


# Usage

ARANGO_DB_HOST = os.getenv("ARANGO_DB_HOST", "")
ARANGO_DB_PORT = os.getenv("ARANGO_DB_PORT", "")

ARANGO_DB_USER = os.getenv("ARANGO_DB_USER", "")
ARANGO_DB_PASSWORD = os.getenv("ARANGO_DB_PASSWORD", "")

ARANGO_DB_NAME = os.getenv("ARANGO_DB_NAME", "")

ARANGO_PGRAPH_DB_NAME = os.getenv("ARANGO_PGRAPH_DB_NAME", "")
ARANGO_PGRAPH_DB_NAME = "Induced"

ARANGO_OGRAPH_NAME = os.getenv("ARANGO_OGRAPH_NAME", "")
ARANGO_PGRAPH_NAME = os.getenv("ARANGO_PGRAPH_NAME", "")

client = ArangoClient(hosts=f"http://{ARANGO_DB_HOST}:{ARANGO_DB_PORT}")
db = client.db(ARANGO_DB_NAME, username=ARANGO_DB_USER, password=ARANGO_DB_PASSWORD)

sys_db = client.db("_system", username=ARANGO_DB_USER, password=ARANGO_DB_PASSWORD)
if not sys_db.has_database(ARANGO_PGRAPH_DB_NAME):
    sys_db.create_database(ARANGO_PGRAPH_DB_NAME)
target_db = client.db(
    ARANGO_PGRAPH_DB_NAME, username=ARANGO_DB_USER, password=ARANGO_DB_PASSWORD
)

build_induced_subgraph(
    db=db,
    graph_name=ARANGO_OGRAPH_NAME,
    source_collection="CS",
    target_db=target_db,
    subgraph_name=ARANGO_PGRAPH_NAME,
)
