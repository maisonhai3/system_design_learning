# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""01 — The symmetry trap: cosine similarity cannot be two-sided."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from lab import db  # noqa: E402
from lab.embed import from_pgvector  # noqa: E402
from lab.harness import Scenario, exit_with, spearman  # noqa: E402
from match.engine import Options, candidate_wants, employer_wants, harmonic  # noqa: E402
from match.query import load_candidate, load_job, suggest_candidates, suggest_jobs  # noqa: E402

s = Scenario(
    "01 — THE SYMMETRY TRAP",
    "cos(resume, jd) == cos(jd, resume). So what, exactly, is two-sided about it?",
)

with db.connect() as conn:
    db.require_embeddings(conn)

    # -----------------------------------------------------------------------
    s.section("The claim: the score matrix is exactly symmetric")
    s.note("""
        Pull the vectors out and compare S[c][j] against S[j][c] directly.
        This is not an approximation that happens to be close. The dot product
        of two vectors does not depend on which one you wrote first, so the
        two numbers are bit-identical.
    """)

    cand_rows = conn.execute(
        "SELECT candidate_id, embedding::text FROM resumes "
        "WHERE embedding IS NOT NULL ORDER BY candidate_id LIMIT 60"
    ).fetchall()
    job_rows = conn.execute(
        "SELECT job_id, embedding::text FROM job_docs "
        "WHERE embedding IS NOT NULL ORDER BY job_id LIMIT 60"
    ).fetchall()

    C = np.vstack([from_pgvector(r[1]) for r in cand_rows])
    J = np.vstack([from_pgvector(r[1]) for r in job_rows])

    forward = C @ J.T          # "how similar is this job to this resume"
    backward = (J @ C.T).T     # "how similar is this resume to this job"
    max_diff = float(np.abs(forward - backward).max())

    s.fact("matrix shape", f"{forward.shape[0]} candidates x {forward.shape[1]} jobs")
    s.fact("max |S_forward - S_backward|", f"{max_diff:.2e}")

    s.broke(
        "the two directions are the same number",
        max_diff < 1e-6,
        "There is one relation here, not two. Reading it by row or by column "
        "does not make the system two-sided any more than reading a distance "
        "matrix by column invents a new kind of distance.",
    )

    # -----------------------------------------------------------------------
    s.section("Why that is a problem and not a curiosity")
    s.note("""
        A symmetric score cannot represent disagreement, and disagreement is
        the normal state of a job market. The clearest case: a candidate who
        is exactly what the job describes, and a job that pays a quarter of
        what the candidate needs. Both parties read the same document pair.
        They should not reach the same conclusion about it.
    """)

    # Search for the pair the two sides disagree about MOST, rather than
    # hand-picking one. A hand-picked example proves the example; a search
    # proves the property, and it fails loudly if the property stops holding.
    disagreements = []
    cand_ids = [r[0] for r in conn.execute(
        "SELECT id FROM candidates ORDER BY id LIMIT 40").fetchall()]
    for cid in cand_ids:
        cand = load_candidate(conn, cid)
        rows = conn.execute(
            """
            SELECT j.id, (1 - (d.embedding <=> (SELECT embedding FROM resumes WHERE candidate_id = %s)))
            FROM job_docs d JOIN jobs j ON j.id = d.job_id
            WHERE j.status = 'open'
            ORDER BY d.embedding <=> (SELECT embedding FROM resumes WHERE candidate_id = %s)
            LIMIT 40
            """,
            (cid, cid),
        ).fetchall()
        for job_id, sim in rows:
            job = load_job(conn, job_id)
            want_c, _ = candidate_wants(cand, job)
            want_e, _ = employer_wants(cand, job)
            disagreements.append((want_e - want_c, cand, job, float(sim), want_c, want_e))

    disagreements.sort(key=lambda t: -t[0])
    gap, cand, job, sim, want_c, want_e = disagreements[0]

    s.fact("candidate", f"{cand['full_name']} — floor {cand['min_salary']:,}, "
                        f"{cand['years_exp']:g}y {cand['seniority']}")
    s.fact("job", f"{job['title'][:44]} — {job['company'][:24]}")
    s.fact("job pays up to", f"{job['salary_max']:,}" if job["salary_max"] else "not stated")
    s.table(
        ["reading", "score", "what it says"],
        [
            ["cosine (symmetric)", f"{sim:.3f}", "one number, offered to both parties"],
            ["employer wants them", f"{want_e:.3f}", "qualified, would interview"],
            ["they want the job", f"{want_c:.3f}", "fails their non-negotiables"],
            ["harmonic mean", f"{harmonic(want_c, want_e):.3f}", "pairing should not happen"],
        ],
        widths=[22, 8, 40],
    )
    s.held(
        "an asymmetric model separates the two readings",
        gap > 0.25,
        f"Widest disagreement found over {len(disagreements)} scored pairs: "
        f"employer {want_e:.2f} vs candidate {want_c:.2f}, a gap of {gap:.2f} "
        "that a single cosine number cannot express.",
    )

    # -----------------------------------------------------------------------
    s.section("What changes in the actual rankings")
    s.note("""
        Rank the same candidate's jobs under cosine and under the two-sided
        score, and compare the orderings. If two-sidedness were cosmetic, the
        rank correlation would be near 1.0 and the top-10 would barely move.
    """)

    base = Options(limit=25, pool=300, filters="off", scoring="cosine", explain=False)
    two_sided = Options(limit=25, pool=300, filters="off", scoring="reciprocal", explain=False)

    moved, correlations = [], []
    for cid in (1, 2, 3, 4, 20, 55, 120, 300):
        a = suggest_jobs(conn, cid, base)
        b = suggest_jobs(conn, cid, two_sided)
        rank_a = {m.id: i for i, m in enumerate(a.matches)}
        # Compare only jobs both rankings returned, or the correlation is
        # measuring set difference rather than ordering.
        shared = [m.id for m in b.matches if m.id in rank_a]
        if len(shared) < 5:
            continue
        rho = spearman(
            [rank_a[j] for j in shared],
            [i for i, m in enumerate(b.matches) if m.id in rank_a],
        )
        top_a = [m.id for m in a.matches[:10]]
        top_b = [m.id for m in b.matches[:10]]
        overlap = len(set(top_a) & set(top_b))
        correlations.append(rho)
        moved.append([cid, f"{rho:+.2f}", f"{overlap}/10", a.matches[0].id, b.matches[0].id])

    s.table(
        ["candidate", "rank corr", "top-10 kept", "cos #1", "two-sided #1"],
        moved,
        widths=[10, 10, 12, 8, 12],
    )

    mean_rho = sum(correlations) / len(correlations)
    s.fact("mean rank correlation", f"{mean_rho:+.3f}")
    s.held(
        "the two-sided ranking is genuinely a different ranking",
        mean_rho < 0.75,
        f"Rank correlation {mean_rho:+.2f} against the cosine ordering. If this "
        "were near 1.0 the extra machinery would be decoration.",
    )

    # -----------------------------------------------------------------------
    s.section("The mirror: same pair, both directions")
    s.note("""
        Ask for a candidate's top job, then ask that job for its top candidates,
        and see whether the original candidate comes back. Under a symmetric
        score people expect this to round-trip. It does not, and the reason is
        worth being clear about: the two queries rank different POPULATIONS.
        Being your top job says nothing about competing against 400 other
        applicants for it.
    """)

    round_trips = []
    for cid in (4, 20, 55, 120, 300):
        res = suggest_jobs(conn, cid, Options(limit=1, pool=300, filters="off",
                                              scoring="cosine", explain=False))
        if not res.matches:
            continue
        top_job = res.matches[0].id
        back = suggest_candidates(conn, top_job, Options(limit=50, pool=400, filters="off",
                                                         scoring="cosine", explain=False))
        ids = [m.id for m in back.matches]
        pos = ids.index(cid) + 1 if cid in ids else None
        round_trips.append([cid, top_job, pos if pos else "not in top 50"])

    s.table(["candidate", "their #1 job", "their rank at that job"], round_trips,
            widths=[12, 14, 24])

    mutual = sum(1 for r in round_trips if r[2] == 1)
    s.held(
        "a symmetric score still does not give mutual top-1",
        mutual < len(round_trips),
        f"{mutual}/{len(round_trips)} pairs are each other's first choice. The "
        "relation is symmetric; the competition is not.",
    )

exit_with(s)
