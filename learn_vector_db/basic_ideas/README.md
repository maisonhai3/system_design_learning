# Learn Vector DB

A tiny, self-contained Python project for learning the basics of a vector database.

## What this teaches

- turning text into vectors with a simple embedding model
- storing documents with metadata
- similarity search with cosine distance
- filtering results by metadata
- upserting records by id

## Project files

- `vector_db.py` - the in-memory vector store and embedding helpers
- `demo.py` - a small runnable demo with sample documents
- `test_vector_db.py` - unit tests that exercise the core behavior

## Run the demo

From the repository root:

```bash
python -m learn_vector_db.demo
```

## Run the tests

```bash
python -m unittest discover -s learn_vector_db -p "test_*.py"
```

## Things to try next

1. Add more documents and see how the ranking changes.
2. Add new metadata fields such as `language`, `author`, or `category`.
3. Change the tokenizer in `vector_db.py` to experiment with different search behavior.
4. Replace the simple embedding model with a real embedding API or a local model.
5. Swap the in-memory store for a real vector database such as Chroma, Qdrant, or pgvector.

