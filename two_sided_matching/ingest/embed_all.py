# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""Embed every resume and job posting that needs it.

    ./lab.sh embed                       # only rows whose text or model changed
    ./lab.sh embed --backend minilm      # switch models (re-embeds everything)
    ./lab.sh embed --force               # re-embed regardless

Two things make this idempotent, and they are the two things people leave out:

  content_hash covers the TEXT AND THE MODEL, so switching backends invalidates
  every row instead of leaving half the corpus in the old vector space.

  For corpus-fitted backends the model id includes a hash of the fit, so
  loading new documents -- which changes IDF, which moves every vector --
  also invalidates every row. See lab/embed.py:fit_signature.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lab import db  # noqa: E402
from lab.embed import content_hash, get_embedder, to_pgvector  # noqa: E402

BATCH = 500


def _fetch_corpus(conn) -> list[str]:
    """Every document, for fitting. Both sides, one vocabulary.

    Fitting on jobs alone would give resume-only vocabulary an IDF of "unseen",
    and unseen terms are dropped -- so the words candidates use about
    themselves would vanish from their own vectors.
    """
    rows = conn.execute("SELECT raw_text FROM job_docs").fetchall()
    rows += conn.execute("SELECT raw_text FROM resumes").fetchall()
    return [r[0] for r in rows]


def _job_docs(conn) -> list[tuple]:
    return conn.execute(
        """
        SELECT d.job_id, j.title, COALESCE(d.parsed->>'skills', ''), d.raw_text,
               d.content_hash
        FROM job_docs d JOIN jobs j ON j.id = d.job_id
        ORDER BY d.job_id
        """
    ).fetchall()


def _resumes(conn) -> list[tuple]:
    return conn.execute(
        """
        SELECT r.candidate_id,
               concat_ws(' ', c.headline, c.current_title),
               COALESCE(r.parsed->>'skills', ''),
               r.raw_text, r.content_hash
        FROM resumes r JOIN candidates c ON c.id = r.candidate_id
        ORDER BY r.candidate_id
        """
    ).fetchall()


def _embed_side(conn, rows, *, table, key_col, embedder, model_id, force, label):
    pending = []
    for key, title, skills, raw_text, stored_hash in rows:
        want = content_hash(raw_text, model_id)
        if not force and stored_hash == want:
            continue
        pending.append((key, {"title": title or "", "skills": skills or "",
                              "body": raw_text}, want))

    if not pending:
        print(f"  {label}: up to date ({len(rows)} rows)")
        return 0

    started = time.time()
    done = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        vectors = embedder.encode_fields([doc for _, doc, _ in chunk])
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                UPDATE {table}
                   SET embedding = %s::vector,
                       content_hash = %s,
                       embedding_model = %s,
                       embedded_at = now()
                 WHERE {key_col} = %s
                """,  # noqa: S608 - table/key are from a fixed internal call site
                [
                    (to_pgvector(vec), want, model_id, key)
                    for (key, _, want), vec in zip(chunk, vectors)
                ],
            )
        conn.commit()
        done += len(chunk)
        db.progress(label, done, len(pending))

    rate = done / max(1e-6, time.time() - started)
    print(f"  {label}: embedded {done}/{len(rows)} ({rate:.0f} docs/sec)")
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default=None, help="hashed-tfidf | bm25-hashed | minilm | openai")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    embedder = get_embedder(args.backend)

    with db.connect() as conn:
        db.require_data(conn)

        corpus = _fetch_corpus(conn)
        print(f"fitting {embedder.name} on {len(corpus)} documents")
        embedder.fit(corpus)
        model_id = embedder.model_id()
        vocab = getattr(embedder, "vocab_size", None)
        print(f"  model id: {model_id}" + (f"   vocab: {vocab}" if vocab else ""))

        total = 0
        total += _embed_side(conn, _job_docs(conn), table="job_docs", key_col="job_id",
                             embedder=embedder, model_id=model_id, force=args.force,
                             label="jobs")
        total += _embed_side(conn, _resumes(conn), table="resumes", key_col="candidate_id",
                             embedder=embedder, model_id=model_id, force=args.force,
                             label="resumes")

        missing = conn.execute(
            "SELECT (SELECT count(*) FROM job_docs WHERE embedding IS NULL),"
            "       (SELECT count(*) FROM resumes  WHERE embedding IS NULL)"
        ).fetchone()

    if missing[0] or missing[1]:
        print(f"\nWARNING: {missing[0]} jobs and {missing[1]} resumes still have no vector.")
        return 1
    print(f"\n{total} documents embedded. Next: ./lab.sh suggest-jobs 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
