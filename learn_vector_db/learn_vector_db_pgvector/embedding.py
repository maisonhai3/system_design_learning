from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DocumentSeed:
    """Sample content plus the fixed embedding we store in pgvector."""

    slug: str
    title: str
    body: str
    embedding: tuple[float, float, float]
    metadata: dict[str, str] = field(default_factory=dict)


class KeywordEmbedding:
    """A tiny, deterministic embedding model for learning.

    It maps text into three dimensions:
    - database / relational concepts
    - vector / semantic search concepts
    - caching / fast lookup concepts

    This is intentionally simple so you can focus on pgvector itself.
    """

    database_terms = {
        "database",
        "databases",
        "postgres",
        "postgresql",
        "sql",
        "table",
        "tables",
        "row",
        "rows",
        "index",
        "indexes",
    }
    vector_terms = {
        "vector",
        "vectors",
        "embedding",
        "embeddings",
        "semantic",
        "similarity",
        "search",
        "query",
    }
    cache_terms = {
        "cache",
        "caching",
        "redis",
        "memory",
        "latency",
        "fast",
        "lookup",
    }

    def encode(self, text: str) -> tuple[float, float, float]:
        tokens = {token.strip(".,:;!?()[]{}\"'").lower() for token in text.split()}
        return (
            float(len(tokens & self.database_terms)),
            float(len(tokens & self.vector_terms)),
            float(len(tokens & self.cache_terms)),
        )

