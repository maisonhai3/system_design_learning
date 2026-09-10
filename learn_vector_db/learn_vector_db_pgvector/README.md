# Learn Vector DB with pgvector

A small, real PostgreSQL + pgvector sandbox for learning how vector search works.

This project keeps the embedding model intentionally simple so you can focus on the database pieces:

- a `vector(3)` column in Postgres
- cosine similarity search with `ORDER BY embedding <=> query`
- a GIN-friendly metadata filter pattern
- a seed dataset you can inspect and change
- a tiny Python runner that prints ranked results

## What you need

- Docker
- Python 3.10+
- `psql` is optional, but handy for poking around

## Quick start

Start Postgres with pgvector:

```bash
cd learn_vector_db_pgvector
docker compose up -d
```

Install the Python package in editable mode:

```bash
python -m pip install -e .
```

Run the demo:

```bash
python -m learn_vector_db_pgvector.demo
```

## Connect manually

Default connection settings:

| setting | value |
|---|---|
| host | `localhost` |
| port | `5435` |
| user | `postgres` |
| password | `postgres` |
| database | `vector_lab` |

```bash
psql postgresql://postgres:postgres@localhost:5435/vector_lab
```

Try the core vector query by hand:

```sql
SELECT slug, title, 1 - (embedding <=> '[0, 3, 0]'::vector) AS similarity
FROM documents
ORDER BY embedding <=> '[0, 3, 0]'::vector
LIMIT 3;
```

## Files

- `docker-compose.yml` — starts a pgvector-enabled PostgreSQL container
- `schema/01_schema.sql` — extension, table, and index setup
- `schema/02_seed.sql` — sample data with explicit vectors
- `embedding.py` — a tiny deterministic embedding model for the demo
- `db.py` — connection, schema, seed, and search helpers
- `demo.py` — the runnable walkthrough
- `test_embedding.py` — pure-Python tests for the toy embedding model

## Why the model is toy-sized

The goal here is to learn the **database** part of vector search, not to hide it behind an API call or a heavyweight ML stack.

The model maps text into three dimensions:

1. database / relational terms
2. vector / semantic-search terms
3. caching / fast-lookup terms

That keeps the data easy to reason about while still using a real `pgvector` column and real SQL similarity search.

## Try changing it

- add a new document to `SAMPLE_DOCUMENTS`
- change the embedding keywords in `embedding.py`
- add a metadata filter to `search_documents`
- change the index type or `lists` setting in `schema/01_schema.sql`

## Reset everything

```bash
docker compose down -v
```

