# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""Rank the same candidates under each embedder backend, side by side.

    ./lab.sh compare-embedders
    ./lab.sh compare-embedders --backends bm25-hashed,minilm

Reads the corpus once and embeds it in memory per backend, so it does not touch
the vectors stored in the database -- you can run it without re-embedding and
without losing the ones you have.

The number to look at is agreement: if two backends produce the same top 10,
the expensive one is not buying anything on YOUR data, whatever the leaderboards
say. And note that a backend needing a 90MB download has to earn that against
what the offline default already gets.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from lab import db  # noqa: E402
from lab.embed import get_embedder  # noqa: E402
from lab.harness import jaccard  # noqa: E402

PROBES = [1, 2, 3, 4, 20, 55, 120, 300]
TOP_K = 10


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backends", default="hashed-tfidf,bm25-hashed")
    ap.add_argument("--probes", type=int, default=len(PROBES))
    args = ap.parse_args()

    with db.connect() as conn:
        db.require_data(conn)
        jobs = conn.execute(
            """
            SELECT d.job_id, j.title, COALESCE(d.parsed->>'skills',''), d.raw_text
            FROM job_docs d JOIN jobs j ON j.id = d.job_id ORDER BY d.job_id
            """
        ).fetchall()
        cands = conn.execute(
            """
            SELECT r.candidate_id, c.full_name,
                   concat_ws(' ', c.headline, c.current_title),
                   COALESCE(r.parsed->>'skills',''), r.raw_text
            FROM resumes r JOIN candidates c ON c.id = r.candidate_id
            ORDER BY r.candidate_id
            """
        ).fetchall()

    job_docs = [{"title": t, "skills": s, "body": b} for _, t, s, b in jobs]
    job_ids = [j[0] for j in jobs]
    job_titles = {j[0]: j[1] for j in jobs}
    cand_by_id = {c[0]: c for c in cands}
    corpus = [j[3] for j in jobs] + [c[4] for c in cands]

    probes = [p for p in PROBES[: args.probes] if p in cand_by_id]
    results: dict[str, dict[int, list[int]]] = {}

    for name in [b.strip() for b in args.backends.split(",") if b.strip()]:
        try:
            emb = get_embedder(name)
        except (RuntimeError, SystemExit) as exc:
            print(f"{name:14s} unavailable: {str(exc).splitlines()[0]}")
            continue

        t0 = time.perf_counter()
        emb.fit(corpus)
        J = emb.encode_fields(job_docs)
        C = emb.encode_fields([
            {"title": cand_by_id[p][2], "skills": cand_by_id[p][3], "body": cand_by_id[p][4]}
            for p in probes
        ])
        elapsed = time.perf_counter() - t0

        sims = C @ J.T
        results[name] = {
            p: [job_ids[i] for i in np.argsort(-sims[row])[:TOP_K]]
            for row, p in enumerate(probes)
        }
        vocab = getattr(emb, "vocab_size", None)
        print(f"{name:14s} {len(corpus)} docs in {elapsed:5.1f}s"
              + (f"   vocab {vocab}" if vocab else "")
              + f"   mean top-1 cos {float(np.max(sims, axis=1).mean()):.3f}")

    names = list(results)
    if len(names) < 2:
        print("\nNeed at least two available backends to compare.")
        return 0

    print("\nagreement — Jaccard of top-10, averaged over probes")
    header = f"  {'':14s}" + "".join(f"{n[:13]:>14s}" for n in names)
    print(header)
    for a in names:
        row = f"  {a[:14]:14s}"
        for b in names:
            avg = sum(jaccard(results[a][p], results[b][p]) for p in probes) / len(probes)
            row += f"{avg:>14.2f}"
        print(row)

    print(f"\ntop-3 per backend, for {len(probes)} candidates")
    for p in probes:
        print(f"\n  {cand_by_id[p][1]}  (#{p})")
        for name in names:
            titles = " | ".join(job_titles[j][:26] for j in results[name][p][:3])
            print(f"    {name:14s} {titles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
