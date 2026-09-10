from __future__ import annotations

import unittest

from learn_vector_db.demo import build_store
from learn_vector_db.vector_db import Document, VocabularyEmbedding, cosine_similarity


class VectorDbTests(unittest.TestCase):
    def test_cosine_similarity(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0, 1], [1, 0, 1]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_search_returns_best_match_first(self) -> None:
        store = build_store()
        results = store.search("vector similarity search", top_k=2)

        self.assertGreaterEqual(results[0].score, results[1].score)
        self.assertEqual(results[0].document.doc_id, "2")

    def test_filter_fn_limits_results(self) -> None:
        store = build_store()
        results = store.search(
            "database",
            top_k=10,
            filter_fn=lambda document: document.metadata.get("type") == "vector",
        )

        self.assertTrue(results)
        self.assertTrue(all(result.document.metadata.get("type") == "vector" for result in results))

    def test_upsert_replaces_existing_document(self) -> None:
        embedding = VocabularyEmbedding()
        embedding.fit(["hello world", "new content"])
        store = __import__("learn_vector_db.vector_db", fromlist=["InMemoryVectorStore"]).InMemoryVectorStore(embedding)

        store.upsert(Document(doc_id="1", text="hello world"))
        store.upsert(Document(doc_id="1", text="new content"))

        self.assertEqual(len(store.documents), 1)
        self.assertEqual(store.documents[0].text, "new content")


if __name__ == "__main__":
    unittest.main()

