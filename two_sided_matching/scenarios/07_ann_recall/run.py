# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""07 — HNSW: what the index costs, and the filter trap that waits for scale."""

import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab import db  # noqa: E402
from lab.harness import Scenario, exit_with, recall_at_k  # noqa: E402

s = Scenario(
    "07 — ANN RECALL AND THE FILTER TRAP",
    "The index is fine until you add a WHERE clause, and fine after that until you add rows.",
)

K = 20
PROBES = [1, 4, 20, 55, 90, 120, 200, 300, 380, 399]


def timed(conn, sql, params, repeats=5):
    out, spent = None, []
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = conn.execute(sql, params).fetchall()
        spent.append((time.perf_counter() - t0) * 1000)
    return out, statistics.median(spent)


def plan(conn, sql, params) -> str:
    rows = conn.execute("EXPLAIN (COSTS OFF) " + sql, params).fetchall()
    for (line,) in rows:
        if "hnsw" in line.lower():
            return "HNSW index scan"
    for (line,) in rows:
        if "Sort" in line:
            return "filter, then exact sort"
    return "other"


with db.connect(autocommit=True) as conn:
    db.require_embeddings(conn)
    n_jobs = conn.execute(
        "SELECT count(*) FROM job_docs WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    conn.execute("DROP INDEX IF EXISTS job_docs_hnsw")

    VEC = "(SELECT embedding FROM resumes WHERE candidate_id = %s)"
    PLAIN = f"""
        SELECT d.job_id FROM job_docs d
        WHERE d.embedding IS NOT NULL
        ORDER BY d.embedding <=> {VEC} LIMIT {K}
    """
    FILTERED = f"""
        SELECT d.job_id
        FROM job_docs d JOIN jobs j ON j.id = d.job_id
        WHERE d.embedding IS NOT NULL
          AND j.status = 'open' AND j.remote AND j.salary_max >= 150000
        ORDER BY d.embedding <=> {VEC} LIMIT {K}
    """

    # -----------------------------------------------------------------------
    s.section("Ground truth: exact search")
    truth, truth_ms = {}, []
    for cid in PROBES:
        rows, ms = timed(conn, PLAIN, (cid,))
        truth[cid] = [r[0] for r in rows]
        truth_ms.append(ms)
    s.fact("vectors in the table", n_jobs)
    s.fact("median exact latency", f"{statistics.median(truth_ms):.1f} ms")

    # -----------------------------------------------------------------------
    s.section("Build the index")
    t0 = time.perf_counter()
    conn.execute("SET maintenance_work_mem = '512MB'")
    conn.execute(
        "CREATE INDEX job_docs_hnsw ON job_docs "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    build_s = time.perf_counter() - t0
    size = conn.execute("SELECT pg_size_pretty(pg_relation_size('job_docs_hnsw'))").fetchone()[0]
    s.fact("build time", f"{build_s:.1f} s for {n_jobs} vectors")
    s.fact("index size", size)
    s.note("""
        vector_cosine_ops, matching the <=> operator in the query. Build it with
        vector_l2_ops and the planner will not use it for <=> at all -- no error,
        no warning, just the sequential scan you were trying to avoid, now with
        an index to maintain.
    """)

    # -----------------------------------------------------------------------
    s.section("Unfiltered, the index is a good trade")
    rows_out = []
    recall_by_ef = {}
    for ef in (10, 40, 100, 400):
        conn.execute(f"SET hnsw.ef_search = {ef}")
        recalls, times = [], []
        for cid in PROBES:
            got, ms = timed(conn, PLAIN, (cid,))
            recalls.append(recall_at_k(truth[cid], [r[0] for r in got], K))
            times.append(ms)
        r = sum(recalls) / len(recalls)
        recall_by_ef[ef] = r
        rows_out.append([ef, f"{100 * r:5.1f}%", f"{statistics.median(times):6.2f} ms",
                         "#" * int(round(28 * r))])
    s.table(["ef_search", f"recall@{K}", "latency", ""], rows_out, widths=[10, 10, 10, 30])

    s.held(
        "ef_search buys recall back, and you can see the price",
        recall_by_ef[400] > recall_by_ef[10],
        f"recall@{K} rises {100 * recall_by_ef[10]:.0f}% -> "
        f"{100 * recall_by_ef[400]:.0f}% from ef_search 10 to 400. The default of "
        "40 is a guess about your data, not a good value.",
    )

    # -----------------------------------------------------------------------
    s.section("Add a WHERE clause, and Postgres quietly saves you")
    matching = conn.execute(
        "SELECT count(*) FROM jobs WHERE status='open' AND remote AND salary_max >= 150000"
    ).fetchone()[0]
    conn.execute("SET hnsw.ef_search = 40")
    chosen = plan(conn, FILTERED, (4,))
    got, filt_ms = timed(conn, FILTERED, (4,))

    s.fact("rows matching the filter", f"{matching} of {n_jobs} "
                                       f"({100 * matching / n_jobs:.1f}%)")
    s.fact("plan the planner picked", chosen)
    s.fact("rows returned", f"{len(got)} of {K}")
    s.note("""
        The planner did not use the HNSW index at all. With salary_max indexed
        and only a few percent of rows matching, a bitmap scan on jobs followed
        by an exact sort of the survivors is genuinely cheaper -- and it is
        exact. This is the right plan, and Postgres found it without help.

        Which is why the next section exists. Everything above is true of a
        2,000-row development database, and none of it is true of the one you
        deploy to.
    """)
    s.held(
        "at small scale the planner pre-filters and stays exact",
        chosen != "HNSW index scan",
        f"{chosen}, {len(got)}/{K} rows, {filt_ms:.2f} ms. Nothing is wrong here, "
        "and that is the problem: this is the test that passes before the bug.",
    )

    # -----------------------------------------------------------------------
    s.section("The same query at 30x the rows")
    s.note("""
        Inflate the table to about 56,000 vectors by reusing the real
        embeddings -- clustered like real data, unlike uniform random noise,
        which makes ANN behave the way it will in production. Then run the same
        shape of query: a filter matching a few percent, ordered by distance,
        LIMIT 20.
    """)

    conn.execute("DROP TABLE IF EXISTS scale_test")
    conn.execute("""
        CREATE TABLE scale_test (id bigserial PRIMARY KEY, tier int, embedding vector(384))
    """)
    conn.execute("""
        INSERT INTO scale_test (tier, embedding)
        SELECT (random() * 40)::int, embedding
        FROM job_docs, generate_series(1, 30)
        WHERE embedding IS NOT NULL
    """)
    conn.execute("""
        CREATE INDEX scale_hnsw ON scale_test
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)
    n_scale = conn.execute("SELECT count(*) FROM scale_test").fetchone()[0]
    n_tier = conn.execute("SELECT count(*) FROM scale_test WHERE tier = 3").fetchone()[0]

    SCALE_Q = """
        SELECT id FROM scale_test WHERE tier = 3
        ORDER BY embedding <=> (SELECT embedding FROM scale_test WHERE id = %s)
        LIMIT 20
    """
    s.fact("rows", f"{n_scale}   (tier=3 matches {n_tier}, "
                   f"{100 * n_tier / n_scale:.1f}%)")
    s.fact("plan the planner picks now", plan(conn, SCALE_Q, (1,)))

    conn.execute("SET enable_indexscan = off")
    exact_ids = [r[0] for r in conn.execute(SCALE_Q, (1,)).fetchall()]
    conn.execute("SET enable_indexscan = on")

    rows_out = []
    for ef in (40, 100, 400):
        conn.execute(f"SET hnsw.ef_search = {ef}")
        got, ms = timed(conn, SCALE_Q, (1,), repeats=3)
        ids = [r[0] for r in got]
        rows_out.append([ef, f"{len(ids)}/{K}",
                         f"{100 * recall_at_k(exact_ids, ids, K):5.1f}%",
                         f"{ms:6.2f} ms"])
    s.table(["ef_search", "rows returned", f"recall@{K}", "latency"], rows_out,
            widths=[10, 14, 10, 10])

    conn.execute("SET hnsw.ef_search = 40")
    short = len(conn.execute(SCALE_Q, (1,)).fetchall())
    s.broke(
        "the same query returns fewer rows than LIMIT once the index is used",
        short < K,
        f"{short} rows for LIMIT {K}, at the default ef_search=40, while "
        f"{len(exact_ids)} qualifying rows exist. HNSW walks the graph in "
        "distance order and the filter is applied to what comes back; the index "
        "knows nothing about `tier`, so the filter throws most of the "
        "ef_search candidates away and the query runs out. No error is raised. "
        "A short result set is indistinguishable from 'there were not many "
        "matches', which is why this ships.",
    )

    # -----------------------------------------------------------------------
    s.section("Three fixes, in the order to reach for them")

    ann40, ann40_ms = timed(conn, SCALE_Q, (1,), repeats=3)
    ann40_ids = [r[0] for r in ann40]

    conn.execute("SET hnsw.ef_search = 400")
    ef_ids = [r[0] for r in conn.execute(SCALE_Q, (1,)).fetchall()]
    _, ef_ms = timed(conn, SCALE_Q, (1,), repeats=3)
    conn.execute("SET hnsw.ef_search = 40")

    # AS MATERIALIZED is not decoration and leaving it out is its own bug.
    # Before PostgreSQL 12 a CTE was always an optimisation fence; since 12 a
    # plain one is INLINED, so the planner flattens this straight back into the
    # filtered query and picks the HNSW index again -- returning the same 9 rows
    # the "fix" was supposed to fix. The first version of this scenario had
    # exactly that bug, and it looked like the pre-filter idea was wrong rather
    # than the SQL.
    PREFILTER = """
        WITH eligible AS MATERIALIZED (
            SELECT id, embedding FROM scale_test WHERE tier = 3
        )
        SELECT id FROM eligible
        ORDER BY embedding <=> (SELECT embedding FROM scale_test WHERE id = %s)
        LIMIT 20
    """
    INLINED = PREFILTER.replace("AS MATERIALIZED", "AS")
    inlined_ids = [r[0] for r in conn.execute(INLINED, (1,)).fetchall()]
    pre, pre_ms = timed(conn, PREFILTER, (1,), repeats=3)
    pre_ids = [r[0] for r in pre]
    s.fact("same CTE without AS MATERIALIZED",
           f"{len(inlined_ids)}/{K} rows — inlined, back on the index")
    s.fact("with AS MATERIALIZED", f"{len(pre_ids)}/{K} rows")

    conn.execute("""
        CREATE INDEX scale_hnsw_tier3 ON scale_test
        USING hnsw (embedding vector_cosine_ops) WHERE tier = 3
    """)
    part, part_ms = timed(conn, SCALE_Q, (1,), repeats=3)
    part_ids = [r[0] for r in part]

    s.table(
        ["approach", "rows", f"recall@{K}", "latency", "what it costs"],
        [
            ["ANN, ef_search=40", f"{len(ann40_ids)}/{K}",
             f"{100 * recall_at_k(exact_ids, ann40_ids, K):5.1f}%",
             f"{ann40_ms:6.2f} ms", "silently short"],
            ["ANN, ef_search=400", f"{len(ef_ids)}/{K}",
             f"{100 * recall_at_k(exact_ids, ef_ids, K):5.1f}%", f"{ef_ms:6.2f} ms",
             "10x graph walk, still approximate"],
            ["pre-filter CTE + exact", f"{len(pre_ids)}/{K}",
             f"{100 * recall_at_k(exact_ids, pre_ids, K):5.1f}%", f"{pre_ms:6.2f} ms",
             "scans the filtered set"],
            ["partial index on tier=3", f"{len(part_ids)}/{K}",
             f"{100 * recall_at_k(exact_ids, part_ids, K):5.1f}%", f"{part_ms:6.2f} ms",
             "one index per filter combination"],
        ],
        widths=[25, 7, 10, 10, 36],
    )

    s.held(
        "pre-filtering into a CTE is exact and returns a full page",
        len(pre_ids) == K and recall_at_k(exact_ids, pre_ids, K) > 0.999,
        f"{len(pre_ids)}/{K} rows at 100% recall. When the filter is selective "
        "the eligible set is small, so scanning it exactly is cheap -- the ANN "
        "index is least useful in precisely the case where it is most wrong. "
        "Reach for this first; add a partial index only once one filter "
        "combination dominates your traffic, because the combinations multiply "
        "and each one is an index to build and maintain.",
    )

    s.note("""
        The rule this leaves you with: an ANN index answers "what is nearest",
        not "what is nearest among rows satisfying P". Selective P -> filter
        first, search exactly. Loose P -> search approximately, filter after.
        And test with production-sized data, because the small-table plan is a
        different plan and it is the correct one.
    """)

    conn.execute("DROP INDEX IF EXISTS scale_hnsw_tier3")
    conn.execute("DROP TABLE IF EXISTS scale_test")
    conn.execute("SET hnsw.ef_search = 40")

exit_with(s)
