-- Learn Vector DB with pgvector — sample rows.
INSERT INTO documents (slug, title, body, metadata, embedding)
VALUES
    (
        'postgres-basics',
        'Postgres stores rows and transactions',
        'A relational database keeps data in tables and enforces transactional updates.',
        '{"topic": "database", "kind": "relational"}'::jsonb,
        '[4, 0, 0]'::vector
    ),
    (
        'vector-search',
        'Vector search compares meaning',
        'A vector database stores embeddings and finds the closest match by similarity.',
        '{"topic": "search", "kind": "vector"}'::jsonb,
        '[1, 4, 0]'::vector
    ),
    (
        'redis-cache',
        'Redis is good for hot data',
        'Caching keeps the hottest values in memory for fast lookup and lower latency.',
        '{"topic": "cache", "kind": "memory"}'::jsonb,
        '[0, 0, 4]'::vector
    ),
    (
        'semantic-search',
        'Semantic search ranks by meaning',
        'Semantic search uses embeddings so similar ideas can match even if words differ.',
        '{"topic": "search", "kind": "vector"}'::jsonb,
        '[0, 3, 0]'::vector
    ),
    (
        'hybrid-search',
        'Hybrid search mixes filters and vectors',
        'Hybrid retrieval can combine metadata filters, keyword search, and vector similarity.',
        '{"topic": "search", "kind": "hybrid"}'::jsonb,
        '[1, 3, 0]'::vector
    )
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    body = EXCLUDED.body,
    metadata = EXCLUDED.metadata,
    embedding = EXCLUDED.embedding;

