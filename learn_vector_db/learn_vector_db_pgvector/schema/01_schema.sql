-- Learn Vector DB with pgvector — schema.
SET client_min_messages = warning;

-- Reset the public schema so the init scripts stay deterministic and easy to read.
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO PUBLIC;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id         bigserial PRIMARY KEY,
    slug       text NOT NULL UNIQUE,
    title      text NOT NULL,
    body       text NOT NULL,
    metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding  vector(3) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- cosine distance is the usual first query shape for semantic search.
CREATE INDEX documents_embedding_idx
    ON documents
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

CREATE INDEX documents_topic_idx ON documents ((metadata ->> 'topic'));

