# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""05 — Retrieve then rerank: how deep does the pool have to be?"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab import db  # noqa: E402
from lab.harness import Scenario, exit_with, recall_at_k, spearman  # noqa: E402
from match.engine import Options  # noqa: E402
from match.query import suggest_jobs  # noqa: E402

s = Scenario(
    "05 — RETRIEVE, THEN RERANK",
    "The second stage is where the quality is. The pool depth is where the bill is.",
)

SAMPLE = [1, 2, 3, 4, 20, 55, 90, 120, 200, 300]
# What a cross-encoder or an LLM call actually costs per pair. The lab's
# stand-in reranker is instant; this constant is what makes the arithmetic
# honest about the system you would really be running.
REAL_RERANK_MS = 40.0

with db.connect() as conn:
    db.require_embeddings(conn)
    n_jobs = conn.execute("SELECT count(*) FROM jobs WHERE status='open'").fetchone()[0]

    # -----------------------------------------------------------------------
    s.section("Does the reranker actually change anything?")
    s.note("""
        If reranking mostly agrees with retrieval, it is an expensive no-op and
        should be deleted. Measure the disagreement before defending the cost.
    """)

    rows, corrs, churns = [], [], []
    for cid in SAMPLE:
        a = suggest_jobs(conn, cid, Options(limit=20, pool=200, filters="lenient",
                                            scoring="reciprocal", rerank=False, explain=False))
        b = suggest_jobs(conn, cid, Options(limit=20, pool=200, filters="lenient",
                                            scoring="reciprocal", rerank=True, explain=False))
        if not a.matches or a.matches[0].score < 0:
            continue
        rank_a = {m.id: i for i, m in enumerate(a.matches)}
        shared = [m.id for m in b.matches if m.id in rank_a]
        if len(shared) < 5:
            continue
        rho = spearman([rank_a[j] for j in shared], list(range(len(shared))))
        churn = 10 - len({m.id for m in a.matches[:10]} & {m.id for m in b.matches[:10]})
        corrs.append(rho)
        churns.append(churn)
        rows.append([cid, f"{rho:+.2f}", f"{churn}/10", a.matches[0].id, b.matches[0].id])

    s.table(["candidate", "rank corr", "top-10 changed", "before #1", "after #1"], rows,
            widths=[10, 10, 15, 10, 9])
    mean_churn = sum(churns) / len(churns)
    s.fact("mean top-10 positions replaced by reranking", f"{mean_churn:.1f}/10")
    s.held(
        "the second stage earns its place",
        mean_churn >= 2.0,
        f"{mean_churn:.1f} of every 10 results change. Below about 1, delete "
        "the stage and keep the latency.",
    )

    # -----------------------------------------------------------------------
    s.section("Why you cannot just rerank everything")
    s.note(f"""
        The stand-in reranker in this lab is free and instant. A real one --
        cross-encoder or LLM -- costs about {REAL_RERANK_MS:.0f}ms per pair, and
        pairs do not batch away entirely. Here is the bill for one request at
        each pool depth, against a catalogue of {n_jobs} open jobs.
    """)

    rows = []
    for pool in (50, 200, 1000, n_jobs):
        t0 = time.perf_counter()
        suggest_jobs(conn, 4, Options(limit=20, pool=pool, filters="lenient",
                                      scoring="reciprocal", rerank=True, explain=False))
        measured = (time.perf_counter() - t0) * 1000
        projected = pool * REAL_RERANK_MS
        rows.append([pool, f"{measured:7.1f} ms", f"{projected / 1000:8.1f} s",
                     "usable" if projected < 1500 else "not a web request"])
    s.table(["pool depth", "this lab", "with a real reranker", "verdict"], rows,
            widths=[11, 12, 22, 20])

    s.broke(
        "reranking the whole catalogue is not a thing you can do online",
        n_jobs * REAL_RERANK_MS > 5000,
        f"{n_jobs} jobs x {REAL_RERANK_MS:.0f}ms = "
        f"{n_jobs * REAL_RERANK_MS / 1000:.0f}s per request. And this catalogue "
        "is two thousand rows; a real board has millions.",
    )

    # -----------------------------------------------------------------------
    s.section("So how deep must the pool be?")
    s.note("""
        Treat 'rerank every eligible job' as ground truth, then measure how
        much of that ideal top-10 a shallow pool still finds.
    """)

    rows = []
    recalls, eligibles = {}, {}
    for pool in (20, 50, 100, 200, 400, 800):
        got, elig = [], []
        for cid in SAMPLE:
            ideal = suggest_jobs(conn, cid, Options(limit=10, pool=n_jobs, filters="lenient",
                                                    scoring="reciprocal", rerank=True,
                                                    explain=False))
            cheap = suggest_jobs(conn, cid, Options(limit=10, pool=pool, filters="lenient",
                                                    scoring="reciprocal", rerank=True,
                                                    explain=False))
            if not ideal.matches or ideal.matches[0].score < 0:
                continue
            got.append(recall_at_k([m.id for m in ideal.matches],
                                   [m.id for m in cheap.matches], 10))
            elig.append(cheap.eligible)
        recalls[pool] = sum(got) / max(1, len(got))
        eligibles[pool] = sum(elig) / max(1, len(elig))
        rows.append([pool, f"{eligibles[pool]:6.0f}", f"{100 * recalls[pool]:5.1f}%",
                     "#" * int(round(recalls[pool] * 28))])
    s.table(["pool retrieved", "-> eligible", "recall@10", ""], rows, widths=[15, 12, 11, 30])

    s.broke(
        "recall stays poor even at a pool of 800",
        recalls[800] < 0.85,
        f"{100 * recalls[800]:.0f}% at pool 800 -- 43% of the catalogue reranked, "
        "and still missing one result in four. Something other than depth is wrong.",
    )

    # -----------------------------------------------------------------------
    s.section("Why depth cannot fix it")
    s.note("""
        Find where the ideal results actually sit in the retriever's own
        ordering. If a job the reranker loves is at cosine rank 1,032, then no
        pool shallower than 1,032 will ever hand it to the reranker -- and a
        pool that deep costs more than reranking the catalogue.
    """)

    deep_rows = []
    worst = 0
    for cid in SAMPLE[:5]:
        ideal = suggest_jobs(conn, cid, Options(limit=10, pool=n_jobs, filters="lenient",
                                                scoring="reciprocal", rerank=True, explain=False))
        cosine_order = suggest_jobs(conn, cid, Options(limit=n_jobs, pool=n_jobs, filters="off",
                                                       scoring="cosine", rerank=False,
                                                       explain=False))
        pos = {m.id: i for i, m in enumerate(cosine_order.matches)}
        ranks = sorted(pos.get(m.id, n_jobs) for m in ideal.matches)
        worst = max(worst, ranks[-1])
        deep_rows.append([cid, ranks[0], ranks[len(ranks) // 2], ranks[-1]])
    s.table(["candidate", "best", "median", "deepest"], deep_rows, widths=[11, 7, 9, 9])

    s.broke(
        "the retriever buries results the reranker would have picked first",
        worst > 400,
        f"Deepest ideal result sits at cosine rank {worst} of {n_jobs}. A "
        "cascade can only reorder what stage 1 surfaces, so recall is capped by "
        "the retriever no matter how good the reranker is. Deepening the pool "
        "is the expensive non-fix.",
    )

    # -----------------------------------------------------------------------
    s.section("The fix: give stage 1 the signal stage 2 is using")
    s.note("""
        The reranker's main signal is exact skill overlap. The embedding blurs
        that -- 'airflow' becomes a projection into 384 dimensions shared with
        thousands of other terms. So add a second retrieval channel that does
        not blur: take the top half of the pool by vector distance and the top
        half by skill-set intersection, and union them.

        This is what hybrid search is for. Not "vectors plus keywords because
        both are good", but: the two channels fail on DIFFERENT documents, so
        the union has a higher ceiling than either.
    """)

    rows = []
    gains = {}
    for pool in (50, 100, 200, 400):
        vec_r, hyb_r = [], []
        for cid in SAMPLE:
            ideal = suggest_jobs(conn, cid, Options(limit=10, pool=n_jobs, filters="lenient",
                                                    scoring="reciprocal", rerank=True,
                                                    explain=False))
            if not ideal.matches or ideal.matches[0].score < 0:
                continue
            gold = [m.id for m in ideal.matches]
            v = suggest_jobs(conn, cid, Options(limit=10, pool=pool, filters="lenient",
                                                scoring="reciprocal", rerank=True,
                                                retrieval="exact", explain=False))
            h = suggest_jobs(conn, cid, Options(limit=10, pool=pool, filters="lenient",
                                                scoring="reciprocal", rerank=True,
                                                retrieval="hybrid", explain=False))
            vec_r.append(recall_at_k(gold, [m.id for m in v.matches], 10))
            hyb_r.append(recall_at_k(gold, [m.id for m in h.matches], 10))
        v_avg = sum(vec_r) / max(1, len(vec_r))
        h_avg = sum(hyb_r) / max(1, len(hyb_r))
        gains[pool] = (v_avg, h_avg)
        rows.append([pool, f"{100 * v_avg:5.1f}%", f"{100 * h_avg:5.1f}%",
                     f"{100 * (h_avg - v_avg):+5.1f}", "#" * int(round(h_avg * 26))])
    s.table(["pool", "vector only", "hybrid", "delta", ""], rows, widths=[7, 13, 9, 8, 28])

    best_pool = max(gains, key=lambda p: gains[p][1])
    v_avg, h_avg = gains[best_pool]
    s.held(
        "a second retrieval channel raises the ceiling that depth could not",
        h_avg > v_avg + 0.05,
        f"At pool {best_pool}: {100 * v_avg:.0f}% -> {100 * h_avg:.0f}% recall@10 "
        "for one extra SQL branch and no change to the reranker. Compare with "
        "quadrupling the pool, which cost 4x the rerank bill for less.",
    )

exit_with(s)
