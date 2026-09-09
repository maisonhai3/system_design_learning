# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""02 — Hard filters: what NULL means, and who decides."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab import db  # noqa: E402
from lab.harness import Scenario, exit_with  # noqa: E402
from match.engine import Options, eligibility  # noqa: E402
from match.query import load_candidate, suggest_jobs  # noqa: E402

s = Scenario(
    "02 — HARD FILTERS",
    "Every filter is a bet that a column is populated. Here is what the bet pays.",
)

with db.connect() as conn:
    db.require_embeddings(conn)

    # -----------------------------------------------------------------------
    s.section("The data you are actually filtering on")
    s.note("""
        Before writing WHERE salary_max >= :floor, look at how many rows have
        a salary_max at all. This is real scraped data from three public job
        feeds -- the coverage below is what job postings are actually like,
        not an artefact of a lazy parser.
    """)

    total = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    cov = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE salary_max IS NOT NULL),
          count(*) FILTER (WHERE country IS NOT NULL),
          count(*) FILTER (WHERE min_years_exp IS NOT NULL),
          count(*) FILTER (WHERE sponsors_visa),
          count(*) FILTER (WHERE remote)
        FROM jobs
        """
    ).fetchone()
    inferred = conn.execute(
        "SELECT count(*) FROM job_docs WHERE (parsed->>'years_inferred')::boolean"
    ).fetchone()[0]

    s.table(
        ["column", "populated", "coverage"],
        [
            ["salary_max", cov[0], f"{100 * cov[0] / total:5.1f}%"],
            ["country", cov[1], f"{100 * cov[1] / total:5.1f}%"],
            ["min_years_exp", cov[2], f"{100 * cov[2] / total:5.1f}%"],
            ["  of which inferred", inferred, f"{100 * inferred / total:5.1f}%"],
            ["sponsors_visa", cov[3], f"{100 * cov[3] / total:5.1f}%"],
            ["remote", cov[4], f"{100 * cov[4] / total:5.1f}%"],
        ],
        widths=[22, 12, 10],
    )

    salary_cov = cov[0] / total
    s.broke(
        "the column your headline filter needs is mostly empty",
        salary_cov < 0.30,
        f"{100 * salary_cov:.1f}% of postings state a maximum salary. A strict "
        "salary filter is therefore mostly a filter on 'did anyone bother to "
        "write it down'.",
    )

    # -----------------------------------------------------------------------
    s.section("Three answers to 'what does NULL mean', and what each costs")
    s.note("""
        strict  : unknown disqualifies. High precision, and it throws away most
                  of the market -- including jobs that would have been fine.
        lenient : unknown passes. Keeps the market, and recommends jobs that
                  turn out to pay half what the candidate needs.
        off     : no filter at all. This is the tutorial version.

        There is no fourth option where the missing data appears. Pick one,
        and know what you picked.
    """)

    sample = [1, 2, 3, 4, 20, 55, 120, 300, 380]
    rows = []
    totals = {"strict": 0, "lenient": 0, "off": 0}
    for cid in sample:
        counts = {}
        for mode in ("off", "lenient", "strict"):
            res = suggest_jobs(conn, cid, Options(
                limit=1000, pool=400, filters=mode, scoring="reciprocal", explain=False))
            n = res.eligible if res.matches and res.matches[0].score >= 0 else 0
            counts[mode] = n
            totals[mode] += n
        cand = load_candidate(conn, cid)
        rows.append([
            f"{cand['full_name'][:18]}", counts["off"], counts["lenient"], counts["strict"],
            f"{100 * counts['strict'] / max(1, counts['off']):.0f}%",
        ])
    s.table(["candidate", "off", "lenient", "strict", "strict/off"], rows,
            widths=[20, 6, 9, 8, 11])

    s.fact("pool of 400 -> eligible, summed over 9 candidates",
           f"off {totals['off']}   lenient {totals['lenient']}   strict {totals['strict']}")

    survival = totals["strict"] / max(1, totals["off"])
    s.broke(
        "strict filtering deletes most of the market",
        survival < 0.25,
        f"{100 * survival:.1f}% of the retrieved pool survives strict filtering. "
        "Most of those rejections are missing data, not bad matches.",
    )

    # -----------------------------------------------------------------------
    s.section("Why the filter must not be a ranking signal")
    s.note("""
        The tempting shortcut is to skip filtering and let a low preference
        score push ineligible jobs down the list. It does not work, because
        'ineligible' is not a small penalty -- it is a different kind of fact.
        Below: how many INELIGIBLE jobs still make the top 10 when eligibility
        is only a soft signal.
    """)

    leaks = []
    for cid in sample:
        cand = load_candidate(conn, cid)
        res = suggest_jobs(conn, cid, Options(
            limit=10, pool=400, filters="off", scoring="reciprocal", explain=False))
        bad = 0
        for m in res.matches:
            if eligibility(cand, m.meta["job"], "lenient"):
                bad += 1
        leaks.append([cand["full_name"][:18], f"{bad}/10"])
    s.table(["candidate", "ineligible in top 10"], leaks, widths=[20, 22])

    total_bad = sum(int(r[1].split("/")[0]) for r in leaks)
    s.broke(
        "soft scoring lets ineligible results into the top 10",
        total_bad > 0,
        f"{total_bad} ineligible jobs across {len(leaks)} candidates' top-10 lists "
        "when eligibility is only a ranking signal. A job the candidate cannot "
        "legally take is not a slightly worse job.",
    )

    # -----------------------------------------------------------------------
    s.section("The fix: filter as a gate, and explain the rejections")
    s.note("""
        With filters on as a gate, the leak is zero by construction. The part
        worth building is what you show when the gate rejects everything:
        'no results' sends the user away, a list of REASONS tells them which
        preference to relax.
    """)

    leaks_fixed = 0
    for cid in sample:
        cand = load_candidate(conn, cid)
        res = suggest_jobs(conn, cid, Options(
            limit=10, pool=400, filters="lenient", scoring="reciprocal", explain=False))
        for m in res.matches:
            if m.score >= 0 and eligibility(cand, m.meta["job"], "lenient"):
                leaks_fixed += 1

    s.held(
        "gating removes ineligible results entirely",
        leaks_fixed == 0,
        f"{leaks_fixed} ineligible results in the top 10 across all "
        f"{len(sample)} candidates.",
    )

    # Do not assume which candidate strikes out -- find them. The corpus has a
    # handful of postings paying 500k+, so the obvious guess (the candidate
    # with the 400,000 floor) turns out to have matches after all.
    empty = []
    for cid in range(1, 61):
        res = suggest_jobs(conn, cid, Options(
            limit=5, pool=400, filters="strict", scoring="reciprocal", explain=True))
        if res.matches and res.matches[0].score < 0:
            empty.append((cid, res))

    s.fact("candidates seeing an EMPTY page under strict filtering",
           f"{len(empty)}/60  ({100 * len(empty) / 60:.0f}%)")

    s.broke(
        "strict filtering shows a real fraction of users nothing at all",
        len(empty) >= 3,
        f"{len(empty)} of 60 candidates match zero jobs once unknown values "
        "disqualify. Not because no job suits them -- because nobody wrote the "
        "salary down.",
    )

    cid, res = empty[0]
    cand = load_candidate(conn, cid)
    s.fact("example", f"{cand['full_name']} — floor {cand['min_salary']:,}, "
                      f"{'remote-only' if cand['remote_required'] else 'onsite ok'}, "
                      f"{'/'.join(cand['willing_locations'])}")
    s.note("what the UI should show instead of 'no results':")
    for m in res.matches[:4]:
        print(f"      {m.title[:42]:42s}  {'; '.join(m.blockers[:2])}")

    every_row_explained = all(m.blockers for m in res.matches)
    s.held(
        "a rejection carries a reason the user can act on",
        every_row_explained and len(res.matches) > 0,
        "Every rejected row names the constraint that rejected it, so the UI can "
        "say 'these 4 jobs fit your skills but none states a salary' rather than "
        "'no results' -- which is the difference between a user relaxing one "
        "filter and a user leaving.",
    )

exit_with(s)
