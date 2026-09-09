# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""03 — Reciprocal ranking: why the harmonic mean, specifically."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab import db  # noqa: E402
from lab.harness import Scenario, exit_with  # noqa: E402
from match.engine import Options, candidate_wants, employer_wants, harmonic  # noqa: E402
from match.query import load_candidate, suggest_jobs  # noqa: E402

s = Scenario(
    "03 — RECIPROCAL RANKING",
    "Two directional scores, one list. The combiner is the whole design.",
)

SAMPLE = [1, 2, 3, 4, 20, 55, 90, 120, 200, 300, 380]
BOTH_HAPPY = 0.45          # what counts as "this side would say yes"


def arithmetic(a: float, b: float) -> float:
    return (a + b) / 2


with db.connect() as conn:
    db.require_embeddings(conn)

    # -----------------------------------------------------------------------
    s.section("The two combiners on the same numbers")
    s.note("""
        Both means agree when the two sides agree. They differ exactly when
        one side is enthusiastic and the other is not -- which is the case
        that decides whether a recommendation is worth anyone's afternoon.
    """)
    s.table(
        ["candidate wants", "employer wants", "arithmetic", "harmonic", "verdict"],
        [
            [f"{a:.2f}", f"{b:.2f}", f"{arithmetic(a, b):.2f}", f"{harmonic(a, b):.2f}", v]
            for a, b, v in [
                (0.90, 0.90, "both keen — both means agree"),
                (0.60, 0.60, "both lukewarm — both means agree"),
                (1.00, 0.20, "employer says no — only harmonic notices"),
                (0.20, 1.00, "candidate says no — only harmonic notices"),
                (0.05, 0.95, "hopeless — arithmetic still calls it average"),
            ]
        ],
        widths=[16, 15, 11, 10, 34],
    )

    # -----------------------------------------------------------------------
    s.section("What that does to real recommendations")
    s.note("""
        Rank each candidate's jobs by arithmetic mean and by harmonic mean of
        the same two directional scores, then count how many of the top 10 are
        pairings where BOTH sides clear a modest bar. Anything below that bar
        on one side is an application that will not turn into a hire.
    """)

    rows = []
    tot_arith = tot_harm = tot_slots = 0
    for cid in SAMPLE:
        cand = load_candidate(conn, cid)
        res = suggest_jobs(conn, cid, Options(
            limit=400, pool=400, filters="lenient", scoring="reciprocal", explain=False))
        if not res.matches or res.matches[0].score < 0:
            continue

        scored = []
        for m in res.matches:
            job = m.meta["job"]
            wc, _ = candidate_wants(cand, job)
            we, _ = employer_wants(cand, job)
            scored.append((m.id, wc, we))

        top_a = sorted(scored, key=lambda t: -arithmetic(t[1], t[2]))[:10]
        top_h = sorted(scored, key=lambda t: -harmonic(t[1], t[2]))[:10]
        ok_a = sum(1 for _, wc, we in top_a if wc >= BOTH_HAPPY and we >= BOTH_HAPPY)
        ok_h = sum(1 for _, wc, we in top_h if wc >= BOTH_HAPPY and we >= BOTH_HAPPY)
        # A "wasted" slot: one side is keen, the other is clearly not.
        waste_a = sum(1 for _, wc, we in top_a if min(wc, we) < 0.30 and max(wc, we) > 0.70)

        tot_arith += ok_a
        tot_harm += ok_h
        tot_slots += 10
        rows.append([cand["full_name"][:18], f"{ok_a}/10", f"{ok_h}/10", waste_a])

    s.table(["candidate", "arith: both ok", "harm: both ok", "arith one-sided"], rows,
            widths=[20, 15, 14, 16])

    pct_a = 100 * tot_arith / max(1, tot_slots)
    pct_h = 100 * tot_harm / max(1, tot_slots)
    s.fact("mutually-acceptable slots, arithmetic mean", f"{tot_arith}/{tot_slots}  ({pct_a:.0f}%)")
    s.fact("mutually-acceptable slots, harmonic mean", f"{tot_harm}/{tot_slots}  ({pct_h:.0f}%)")

    s.broke(
        "the arithmetic mean fills slots with one-sided pairings",
        tot_arith < tot_harm,
        f"{pct_a:.0f}% of its top-10 slots satisfy both sides, against "
        f"{pct_h:.0f}% for the harmonic mean, on identical inputs.",
    )
    s.held(
        "the harmonic mean ranks mutual fit above one-sided enthusiasm",
        pct_h >= pct_a + 5,
        f"+{pct_h - pct_a:.0f} percentage points of mutually-acceptable "
        "recommendations, for one line of arithmetic.",
    )

    # -----------------------------------------------------------------------
    s.section("The asymmetry is not cosmetic: the same pair, ranked twice")
    s.note("""
        Take a pairing and ask both sides. The gap between the two directional
        scores is what a single-score system has to discard. Below, the widest
        gaps found in one candidate's eligible set.
    """)

    cand = load_candidate(conn, 4)
    res = suggest_jobs(conn, 4, Options(
        limit=400, pool=400, filters="lenient", scoring="reciprocal", explain=False))
    gaps = []
    for m in res.matches:
        job = m.meta["job"]
        wc, _ = candidate_wants(cand, job)
        we, _ = employer_wants(cand, job)
        gaps.append((abs(wc - we), job["title"][:34], wc, we))
    gaps.sort(reverse=True)

    s.fact("candidate", cand["full_name"])
    s.table(
        ["job", "they want it", "employer wants them", "gap"],
        [[t, f"{wc:.2f}", f"{we:.2f}", f"{g:.2f}"] for g, t, wc, we in gaps[:6]],
        widths=[36, 13, 20, 6],
    )

    widest = gaps[0][0] if gaps else 0.0
    s.held(
        "the two directions genuinely disagree on real pairs",
        widest > 0.3,
        f"Widest gap {widest:.2f} within one candidate's eligible set. Averaging "
        "these into one number is throwing away the most informative thing you "
        "computed.",
    )

exit_with(s)
