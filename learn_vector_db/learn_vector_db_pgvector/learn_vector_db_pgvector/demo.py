from __future__ import annotations

from .data import SAMPLE_DOCUMENTS
from .db import count_documents, ensure_schema, open_connection, search_documents, seed_documents
from .embedding import KeywordEmbedding


DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5435/vector_lab"


def build_query_vector(model: KeywordEmbedding, query: str) -> tuple[float, float, float]:
    return model.encode(query)


def main() -> None:
    model = KeywordEmbedding()
    dsn = DEFAULT_DSN
    conn = open_connection(dsn)

    with conn:
        ensure_schema(conn)
        if count_documents(conn) == 0:
            seed_documents(conn, SAMPLE_DOCUMENTS)

        print("Learn Vector DB with pgvector\n")
        print(f"Connected to: {dsn}")
        print(f"Documents in database: {count_documents(conn)}\n")

        print("Sample embeddings:")
        for document in SAMPLE_DOCUMENTS:
            print(f"- {document.slug:16} embedding={document.embedding}  topic={document.metadata['topic']}")

        print("\nSearch examples:")
        for query in [
            "How does vector search work?",
            "What is a cache for fast lookup?",
        ]:
            query_vector = build_query_vector(model, query)
            print(f"\nQuery: {query}")
            print(f"Vector: {query_vector}")
            for row in search_documents(conn, query_vector, limit=3):
                print(
                    f"  similarity={row['similarity']:.3f}  slug={row['slug']}  "
                    f"topic={row['metadata']['topic']}  title={row['title']}"
                )

        print("\nFilter example: only topic='search'")
        query_vector = build_query_vector(model, "meaning based search")
        for row in search_documents(conn, query_vector, limit=3, topic="search"):
            print(
                f"  similarity={row['similarity']:.3f}  slug={row['slug']}  "
                f"topic={row['metadata']['topic']}  title={row['title']}"
            )


if __name__ == "__main__":
    main()

