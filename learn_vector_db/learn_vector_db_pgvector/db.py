from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional, Sequence

from .embedding import DocumentSeed


SCHEMA_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS documents (
        id         bigserial PRIMARY KEY,
        slug       text NOT NULL UNIQUE,
        title      text NOT NULL,
        body       text NOT NULL,
        metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
        embedding  vector(3) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS documents_embedding_idx
        ON documents
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 10)
    """.strip(),
    "CREATE INDEX IF NOT EXISTS documents_topic_idx ON documents ((metadata ->> 'topic'))",
)


def open_connection(dsn: str) -> Any:
    """Open a psycopg connection and register pgvector adapters.

    Importing psycopg here keeps the rest of the project importable even when the
    optional database dependencies are not installed yet.
    """

    try:
        import psycopg
        from psycopg.rows import dict_row
        from pgvector.psycopg import register_vector
    except ImportError as exc:  # pragma: no cover - exercised when deps are missing
        raise RuntimeError(
            "Install the project dependencies first: python -m pip install -e ./learn_vector_db_pgvector"
        ) from exc

    conn = psycopg.connect(dsn, row_factory=dict_row)
    register_vector(conn)
    return conn


def ensure_schema(conn: Any) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


def seed_documents(conn: Any, documents: Iterable[DocumentSeed]) -> None:
    try:
        from pgvector import Vector
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - exercised when deps are missing
        raise RuntimeError("pgvector and psycopg must be installed before seeding documents") from exc

    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO documents (slug, title, body, metadata, embedding)
            VALUES (%(slug)s, %(title)s, %(body)s, %(metadata)s, %(embedding)s)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
            """,
            [
                {
                    "slug": document.slug,
                    "title": document.title,
                    "body": document.body,
                    "metadata": Jsonb(document.metadata),
                    "embedding": Vector(document.embedding),
                }
                for document in documents
            ],
        )


def count_documents(conn: Any) -> int:
    return conn.execute("SELECT count(*) AS count FROM documents").fetchone()["count"]


def search_documents(
    conn: Any,
    query_vector: Sequence[float],
    *,
    limit: int = 3,
    topic: Optional[str] = None,
) -> list[dict[str, object]]:
    try:
        from pgvector import Vector
    except ImportError as exc:  # pragma: no cover - exercised when deps are missing
        raise RuntimeError("pgvector must be installed before searching documents") from exc

    params: dict[str, object] = {"query": Vector(list(query_vector)), "limit": limit}
    topic_filter = ""
    if topic is not None:
        params["topic"] = topic
        topic_filter = "WHERE metadata ->> 'topic' = %(topic)s"

    rows = conn.execute(
        f"""
        SELECT
            slug,
            title,
            body,
            metadata,
            embedding,
            1 - (embedding <=> %(query)s) AS similarity
        FROM documents
        {topic_filter}
        ORDER BY embedding <=> %(query)s
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return rows

