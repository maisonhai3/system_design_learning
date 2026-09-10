from __future__ import annotations

import unittest

from learn_vector_db_pgvector.embedding import KeywordEmbedding


class KeywordEmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = KeywordEmbedding()

    def test_database_dimension_counts_database_words(self) -> None:
        self.assertEqual(self.model.encode("Postgres table rows and SQL index"), (5.0, 0.0, 0.0))

    def test_vector_dimension_counts_vector_words(self) -> None:
        self.assertEqual(self.model.encode("vector embeddings and similarity search"), (0.0, 4.0, 0.0))

    def test_cache_dimension_counts_cache_words(self) -> None:
        self.assertEqual(self.model.encode("Redis cache for fast lookup in memory"), (0.0, 0.0, 5.0))

    def test_mixed_query_hits_multiple_dimensions(self) -> None:
        self.assertEqual(self.model.encode("vector search for postgres"), (1.0, 2.0, 0.0))


if __name__ == "__main__":
    unittest.main()

