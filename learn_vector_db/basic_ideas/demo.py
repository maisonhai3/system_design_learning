from __future__ import annotations

from learn_vector_db.vector_db import Document, InMemoryVectorStore, VocabularyEmbedding


DOCUMENTS = [
    Document(
        doc_id="1",
        text="Postgres stores rows and supports SQL transactions.",
        metadata={"topic": "databases", "type": "relational"},
    ),
    Document(
        doc_id="2",
        text="A vector database stores embeddings and searches by similarity.",
        metadata={"topic": "databases", "type": "vector"},
    ),
    Document(
        doc_id="3",
        text="Redis is often used for caching and fast key-value lookups.",
        metadata={"topic": "databases", "type": "cache"},
    ),
    Document(
        doc_id="4",
        text="Semantic search compares meaning rather than exact words.",
        metadata={"topic": "search", "type": "vector"},
    ),
]


def build_store() -> InMemoryVectorStore:
    embedding = VocabularyEmbedding()
    embedding.fit([document.text for document in DOCUMENTS])
    store = InMemoryVectorStore(embedding)
    for document in DOCUMENTS:
        store.upsert(document)
    return store


def main() -> None:
    store = build_store()
    queries = [
        "How does a vector database search documents?",
        "How is caching used?",
    ]

    print("Learn Vector DB demo\n")
    print("Documents loaded:")
    for document in store.documents:
        print(f"- {document.doc_id}: {document.text}")

    for query in queries:
        print(f"\nQuery: {query}")
        for result in store.search(query, top_k=3):
            print(f"  score={result.score:.3f}  id={result.document.doc_id}  text={result.document.text}")

    print("\nFilter example: only documents tagged as type=vector")
    for result in store.search("similarity search", top_k=3, filter_fn=lambda doc: doc.metadata.get("type") == "vector"):
        print(f"  score={result.score:.3f}  id={result.document.doc_id}  text={result.document.text}")


if __name__ == "__main__":
    main()


