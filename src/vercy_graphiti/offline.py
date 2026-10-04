"""Graphiti with no LLM, no network and no telemetry, for reproducible runs.

- Kuzu embedded in memory as the default graph store (graphiti-core 0.30 still ships the driver,
  deprecated). Set VERCY_NEO4J_URI (plus VERCY_NEO4J_USER / VERCY_NEO4J_PASSWORD) to run the same
  harness on Neo4j, Graphiti's primary backend; CI does both.
- A deterministic hashing embedder: stable across machines, no model download.
- No LLM and no cross-encoder: writes go through `EntityEdge.save`, searches use the
  BM25 + cosine RRF recipe. Any accidental LLM call raises instead of reaching a provider.

Scores from this harness measure governance behaviour, not retrieval quality: a real
embedder will rank differently. The governed arm does not depend on ranking.
"""
from __future__ import annotations

import hashlib
import math
import os

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")


async def open_graphiti():
    """A Graphiti on Neo4j when VERCY_NEO4J_URI is set, otherwise on in-memory Kuzu."""
    uri = os.environ.get("VERCY_NEO4J_URI")
    if not uri:
        return make_graphiti()
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    driver = Neo4jDriver(uri, os.environ.get("VERCY_NEO4J_USER", "neo4j"), os.environ.get("VERCY_NEO4J_PASSWORD"))
    graphiti = make_graphiti(driver)
    await graphiti.build_indices_and_constraints()
    return graphiti


def backend() -> str:
    return "neo4j" if os.environ.get("VERCY_NEO4J_URI") else "kuzu (embedded, in memory)"


def make_graphiti(driver=None):
    import warnings

    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.driver.driver import GraphProvider
    if driver is None:
        import kuzu
        from graphiti_core.driver.kuzu_driver import KuzuDriver
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.graph_queries import get_fulltext_indices
    from graphiti_core.llm_client.client import LLMClient

    class HashEmbedder(EmbedderClient):
        dim = 256

        async def create(self, input_data):
            text = input_data if isinstance(input_data, str) else " ".join(map(str, input_data))
            vec = [0.0] * self.dim
            for token in text.lower().split():
                vec[int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            return [v / norm for v in vec]

        async def create_batch(self, input_data_list):
            return [await self.create(t) for t in input_data_list]

    class NoLLM(LLMClient):
        def __init__(self):
            pass

        def set_tracer(self, tracer):
            pass

        async def _generate_response(self, *args, **kwargs):
            raise RuntimeError("this harness makes no LLM calls")

    class NoRerank(CrossEncoderClient):
        async def rank(self, query, passages):
            raise RuntimeError("this harness makes no reranker calls")

    if driver is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            driver = KuzuDriver(db=":memory:")
        # The Kuzu driver creates its schema but not its full-text indices; search needs them.
        conn = kuzu.Connection(driver.db)
        conn.execute("LOAD EXTENSION FTS;")
        for query in get_fulltext_indices(GraphProvider.KUZU):
            conn.execute(query)
        conn.close()
    return Graphiti(graph_driver=driver, llm_client=NoLLM(), embedder=HashEmbedder(), cross_encoder=NoRerank())
