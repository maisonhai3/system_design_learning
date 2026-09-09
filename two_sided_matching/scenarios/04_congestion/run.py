# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""04 — Congestion: every list is good, the market is broken."""

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab import db  # noqa: E402
from lab.harness import Scenario, bar, coverage, exit_with, gini, jaccard  # noqa: E402
from match.engine import Options  # noqa: E402
from match.query import suggest_jobs  # noqa: E402

s = Scenario(
    "04 — CONGESTION",
    "The failure you cannot see in any single user's results.",
)

TOP_K = 10
N_CANDIDATES = 250


def simulate(conn, opts: Options, n: int) -> tuple[list[list[int]], collections.Counter]:
    """Give the first n candidates their top-K and tally who got recommended."""
    lists, tally = [], collections.Counter()
    for cid in range(1, n + 1):
        res = suggest_jobs(conn, cid, opts)
        ids = [m.id for m in res.matches if m.score >= 0][:TOP_K]
        if ids:
            lists.append(ids)
            tally.update(ids)
    return lists, tally


with db.connect() as conn:
    db.require_embeddings(conn)
    n_jobs = conn.execute("SELECT count(*) FROM jobs WHERE status = 'open'").fetchone()[0]

    # -----------------------------------------------------------------------
    s.section("Per-user metrics look fine")
    s.note("""
        Every candidate gets ten eligible, well-matched jobs. Precision@10
        measured per user is good. Nothing in a per-user dashboard is red.
        This is what makes congestion dangerous: the metric you are watching
        cannot see it, because it aggregates over the wrong axis.
    """)

    base = Options(limit=TOP_K, pool=300, filters="lenient",
                   scoring="reciprocal", congestion=0.0, explain=False)
    lists, tally = simulate(conn, base, N_CANDIDATES)

    s.fact("candidates served", len(lists))
    s.fact("recommendation slots handed out", sum(len(x) for x in lists))
    s.fact("distinct jobs ever recommended", f"{len(tally)} of {n_jobs}")

    # -----------------------------------------------------------------------
    s.section("Aggregate over jobs instead, and the market is a pyramid")

    counts = [tally.get(jid, 0) for jid in range(1, n_jobs + 1)]
    g_before = gini(counts)
    cov_before = coverage(lists, n_jobs)
    top20 = tally.most_common(20)
    share_top20 = sum(c for _, c in top20) / max(1, sum(tally.values()))

    rows = []
    for jid, c in top20[:8]:
        title = conn.execute("SELECT title FROM jobs WHERE id = %s", (jid,)).fetchone()[0]
        rows.append([jid, title[:38], c, bar(c / max(1, top20[0][1]), 22)])
    s.table(["job", "title", "times recommended", ""], rows, widths=[6, 40, 18, 24])

    s.fact("Gini of recommendation counts", f"{g_before:.3f}")
    s.fact("catalogue coverage", f"{100 * cov_before:.1f}%")
    s.fact("share of all slots taken by the top 20 jobs", f"{100 * share_top20:.1f}%")

    s.broke(
        "recommendations concentrate on a small set of jobs",
        g_before > 0.6 and cov_before < 0.5,
        f"Gini {g_before:.2f}, coverage {100 * cov_before:.0f}%. The top 20 "
        f"postings absorb {100 * share_top20:.0f}% of every slot handed out. "
        "Those employers get hundreds of applications they cannot read; the "
        "rest of the catalogue is invisible.",
    )

    # -----------------------------------------------------------------------
    s.section("And the identical twins get identical lists")
    s.note("""
        Candidates 4 and 5 have near-identical resumes on purpose. Whatever
        arbitrary tiebreak separates them is stable across every query, so one
        of them is permanently second -- for every job, forever. A ranker that
        is deterministic in a market with scarce slots does not distribute
        opportunity, it assigns it once.
    """)
    a = [m.id for m in suggest_jobs(conn, 4, base).matches]
    b = [m.id for m in suggest_jobs(conn, 5, base).matches]
    twin_overlap = jaccard(a, b)
    s.fact("Jaccard(top-10 of twins)", f"{twin_overlap:.2f}")
    s.broke(
        "near-identical candidates receive near-identical lists",
        twin_overlap > 0.5,
        f"Jaccard {twin_overlap:.2f}. They will compete with each other, and "
        "only each other, on every single application.",
    )

    # -----------------------------------------------------------------------
    s.section("The obvious fix, which does not work")
    s.note("""
        Penalise jobs that already have a lot of applications:
        1 / (1 + strength * log1p(applicants / headcount)). It is the right
        shape -- logarithmic, so 5 vs 50 applicants matters more than 300 vs
        350 -- and it is reading the wrong number.
    """)

    rows = []
    for strength in (0.0, 1.0, 4.0):
        opts = Options(limit=TOP_K, pool=300, filters="lenient",
                       scoring="reciprocal", congestion=strength, explain=False)
        lists_c, tally_c = simulate(conn, opts, N_CANDIDATES)
        counts_c = [tally_c.get(jid, 0) for jid in range(1, n_jobs + 1)]
        rows.append([f"{strength:.1f}", f"{gini(counts_c):.3f}",
                     f"{100 * coverage(lists_c, n_jobs):5.1f}%"])
    s.table(["congestion", "Gini", "coverage"], rows, widths=[11, 8, 10])

    g_hist = float(rows[-1][1])
    s.broke(
        "a penalty on HISTORICAL applications does not fix concentration",
        g_hist >= g_before - 0.02,
        f"Gini {g_before:.3f} -> {g_hist:.3f}: no improvement at any strength. "
        "The applications table records what users did last month; the pile-up "
        "being measured is what the ranker is doing right now, to users who "
        "have not applied to anything yet. The penalty is reading a number "
        "that does not know about the problem.",
    )

    # -----------------------------------------------------------------------
    s.section("The fix that works, and what it costs you")
    s.note("""
        Feed the penalty from what THIS serving round has already handed out.
        Keep a running tally as you generate lists, and let each candidate's
        ranking see how crowded each job already is.

        Note what just happened to the architecture. The recommender is no
        longer a pure function of (user, catalogue): serving user 200 depends
        on what users 1..199 were shown. That buys fairness and costs you
        statelessness -- the tally is shared mutable state, so it needs a home
        (Redis, a sliding window), it needs to expire, and two servers running
        without it will both flood the same job. A cross-user problem cannot be
        fixed by a per-user function. That is the whole lesson.
    """)

    rows = []
    best = None
    for strength in (0.0, 1.0, 3.0, 8.0):
        tally_live: collections.Counter = collections.Counter()
        lists_live = []
        for cid in range(1, N_CANDIDATES + 1):
            opts = Options(limit=TOP_K, pool=300, filters="lenient",
                           scoring="reciprocal", congestion=strength,
                           serving_load=dict(tally_live), explain=False)
            res = suggest_jobs(conn, cid, opts)
            ids = [m.id for m in res.matches if m.score >= 0][:TOP_K]
            if ids:
                lists_live.append(ids)
                tally_live.update(ids)

        counts_l = [tally_live.get(jid, 0) for jid in range(1, n_jobs + 1)]
        g = gini(counts_l)
        cov = coverage(lists_live, n_jobs)
        kept = sum(jaccard(x, y) for x, y in zip(lists, lists_live)) / max(1, len(lists))
        rows.append([f"{strength:.1f}", f"{g:.3f}", f"{100 * cov:5.1f}%",
                     f"{100 * kept:5.1f}%", bar(1 - g, 20)])
        if strength == 3.0:
            best = (g, cov, kept)

    s.table(["feedback", "Gini", "coverage", "top-10 kept", "evenness"], rows,
            widths=[10, 8, 10, 12, 22])

    g_after, cov_after, kept_after = best
    s.held(
        "feedback congestion spreads recommendations across the catalogue",
        g_after < g_before - 0.05 and cov_after > cov_before + 0.05,
        f"Gini {g_before:.3f} -> {g_after:.3f}, coverage "
        f"{100 * cov_before:.0f}% -> {100 * cov_after:.0f}%, keeping "
        f"{100 * kept_after:.0f}% of the original top 10. That last number is "
        "the price. Whether it is worth paying is a product decision, not a "
        "modelling one -- but you cannot even have the argument until the "
        "penalty reads the right number.",
    )

exit_with(s)
