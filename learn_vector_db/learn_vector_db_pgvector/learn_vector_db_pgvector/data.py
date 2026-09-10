from __future__ import annotations

from .embedding import DocumentSeed


SAMPLE_DOCUMENTS: tuple[DocumentSeed, ...] = (
    DocumentSeed(
        slug="postgres-basics",
        title="Postgres stores rows and transactions",
        body="A relational database keeps data in tables and enforces transactional updates.",
        embedding=(4.0, 0.0, 0.0),
        metadata={"topic": "database", "kind": "relational"},
    ),
    DocumentSeed(
        slug="vector-search",
        title="Vector search compares meaning",
        body="A vector database stores embeddings and finds the closest match by similarity.",
        embedding=(1.0, 4.0, 0.0),
        metadata={"topic": "search", "kind": "vector"},
    ),
    DocumentSeed(
        slug="redis-cache",
        title="Redis is good for hot data",
        body="Caching keeps the hottest values in memory for fast lookup and lower latency.",
        embedding=(0.0, 0.0, 4.0),
        metadata={"topic": "cache", "kind": "memory"},
    ),
    DocumentSeed(
        slug="semantic-search",
        title="Semantic search ranks by meaning",
        body="Semantic search uses embeddings so similar ideas can match even if words differ.",
        embedding=(0.0, 3.0, 0.0),
        metadata={"topic": "search", "kind": "vector"},
    ),
    DocumentSeed(
        slug="hybrid-search",
        title="Hybrid search mixes filters and vectors",
        body="Hybrid retrieval can combine metadata filters, keyword search, and vector similarity.",
        embedding=(1.0, 3.0, 0.0),
        metadata={"topic": "search", "kind": "hybrid"},
    ),
)

