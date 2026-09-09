# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""06 — Cold start: the new job nobody has applied to yet."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab import db  # noqa: E402
from lab.embed import content_hash, get_embedder, to_pgvector  # noqa: E402
from lab.harness import Scenario, exit_with  # noqa: E402
from lab.text import extract_skills  # noqa: E402
from match.engine import Options  # noqa: E402
from match.query import suggest_candidates, suggest_jobs  # noqa: E402

s = Scenario(
    "06 — COLD START",
    "Content-based retrieval has one great virtue: it works on day zero.",
)

NEW_JOB = """
Senior Data Platform Engineer — Remote (Japan / worldwide)

We are looking for someone to own our batch and streaming pipelines end to end.
The stack is airflow for orchestration, dbt for the warehouse models, kafka for
event ingestion, and snowflake underneath. You would be the third data engineer
on a team of twelve, with full ownership of the ingestion path.

Requirements: 5+ years building production data pipelines, strong python and
sql, experience running airflow at scale. Remote, anywhere with four hours of
overlap with JST. 140,000 - 190,000 USD depending on experience.
"""

with db.connect() as conn:
    db.require_embeddings(conn)

    # -----------------------------------------------------------------------
    s.section("What a brand new posting has, and what it does not")
    s.note("""
        Insert a job that has never been seen, clicked, saved or applied to.
        Collaborative filtering -- "people who applied to this also applied
        to that" -- has literally nothing to work with: the row does not
        appear in the interactions table at all. Everything popularity-ranked
        has the same problem, which is why a pure behavioural recommender
        cannot launch a marketplace, only grow one.
    """)

    # Reuse the model already in the table. Embedding a new document with a
    # different model, or with a different fit of the same model, puts it in a
    # vector space the rest of the corpus does not share -- it would come back
    # as similar to nothing at all, which looks like a relevance bug.
    model_id = conn.execute(
        "SELECT embedding_model FROM job_docs WHERE embedding_model IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    backend = model_id.split("@")[0]
    embedder = get_embedder(backend)
    corpus = [r[0] for r in conn.execute("SELECT raw_text FROM job_docs").fetchall()]
    corpus += [r[0] for r in conn.execute("SELECT raw_text FROM resumes").fetchall()]
    embedder.fit(corpus)

    if embedder.model_id() != model_id:
        s.note(f"refit signature differs ({embedder.model_id()} vs {model_id}); "
               "comparing within this run only.")

    with conn.cursor() as cur:
        emp_id = cur.execute(
            "INSERT INTO employers (name, company_size) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            ("Coldstart Data Co", 12),
        ).fetchone()[0]
        job_id = cur.execute(
            """
            INSERT INTO jobs (employer_id, title, status, location, country, remote,
                              employment, salary_min, salary_max, min_years_exp,
                              seniority, headcount, source, source_id)
            VALUES (%s,%s,'open',%s,'WW',true,'fulltime',140000,190000,5.0,'senior',1,
                    'synthetic','coldstart-1')
            ON CONFLICT (source, source_id) DO UPDATE SET title = EXCLUDED.title
            RETURNING id
            """,
            (emp_id, "Senior Data Platform Engineer", "Remote (Japan / worldwide)"),
        ).fetchone()[0]
        skills = extract_skills(NEW_JOB)
        vec = embedder.encode_fields([{
            "title": "Senior Data Platform Engineer",
            "skills": " ".join(skills),
            "body": NEW_JOB,
        }])[0]
        cur.execute(
            """
            INSERT INTO job_docs (job_id, raw_text, parsed, embedding, content_hash,
                                  embedding_model, embedded_at)
            VALUES (%s,%s,%s,%s::vector,%s,%s,now())
            ON CONFLICT (job_id) DO UPDATE SET
                raw_text = EXCLUDED.raw_text, parsed = EXCLUDED.parsed,
                embedding = EXCLUDED.embedding, content_hash = EXCLUDED.content_hash,
                embedding_model = EXCLUDED.embedding_model
            """,
            (job_id, NEW_JOB, '{"skills": %s, "repost_count": 1}' % (
                str(skills).replace("'", '"')), to_pgvector(vec),
             content_hash(NEW_JOB, model_id), model_id),
        )
    conn.commit()

    n_inter = conn.execute(
        "SELECT count(*) FROM interactions WHERE job_id = %s", (job_id,)
    ).fetchone()[0]
    s.fact("new job id", job_id)
    s.fact("skills parsed out of it", ", ".join(skills[:8]))
    s.fact("rows in interactions", n_inter)

    s.broke(
        "a behavioural signal does not exist for a new listing",
        n_inter == 0,
        "Zero impressions, clicks, saves or applications. Any ranker whose "
        "input is behaviour scores this job at exactly nothing, forever, "
        "because it needs traffic to earn traffic.",
    )

    # -----------------------------------------------------------------------
    s.section("Content-based retrieval finds it immediately")

    res = suggest_candidates(conn, job_id, Options(
        limit=10, pool=400, filters="lenient", scoring="reciprocal", explain=True))
    rows = [[m.id, m.title[:20], f"{m.sim:.3f}", f"{m.score:.3f}",
             ", ".join((m.meta["cand"].get("skills") or [])[:4])]
            for m in res.matches[:6]]
    s.table(["cand", "name", "cosine", "score", "skills"], rows, widths=[6, 22, 8, 8, 34])

    # Candidates 4 and 5 are the pinned data-engineering twins.
    found_twins = {m.id for m in res.matches} & {4, 5}
    s.held(
        "the right candidates surface with zero interaction history",
        bool(found_twins),
        f"The pinned data-engineering candidates {sorted(found_twins)} appear in "
        "the top 10 on the day the job was posted. The document is the signal; "
        "no traffic required.",
    )

    # -----------------------------------------------------------------------
    s.section("A tempting claim, and why it is false")
    s.note("""
        The obvious next thought: a new job has no applicants, so the
        congestion penalty from scenarios/04 leaves it alone while shrinking
        every crowded incumbent -- fairness machinery doubling as cold-start
        promotion, two problems for one term.

        It sounded right enough to build. It is wrong, and the measurement
        below is what wrongness looks like. Kept in the lab rather than
        deleted, because "the mechanism I added for A probably also fixes B"
        is a claim to test, not a bonus to assume.
    """)

    # Rank is the wrong instrument -- the new posting is already #1 for the
    # only candidates who retrieve it, so it cannot rise. Measure the MARGIN
    # over the runner-up. And use the feedback tally, since scenarios/04
    # established that the historical applications table does not move.
    import collections as _c

    tally: _c.Counter = _c.Counter()
    for cid in range(1, 200):
        res_w = suggest_jobs(conn, cid, Options(
            limit=10, pool=300, filters="lenient", scoring="reciprocal",
            congestion=2.0, serving_load=dict(tally), explain=False))
        tally.update(m.id for m in res_w.matches if m.score >= 0)

    s.fact("serving round: slots handed out", sum(tally.values()))
    s.fact("load accumulated by the NEW job", tally.get(job_id, 0))
    s.fact("load on the busiest incumbent", tally.most_common(1)[0][1])

    watchers = [4, 5, 20, 55, 90, 120]
    rows, widened, seen = [], 0, 0
    for cid in watchers:
        margins = {}
        for label, load in (("off", None), ("feedback", dict(tally))):
            res_c = suggest_jobs(conn, cid, Options(
                limit=60, pool=400, filters="lenient", scoring="reciprocal",
                congestion=0.0 if load is None else 2.0,
                serving_load=load, explain=False))
            ms = [m for m in res_c.matches if m.score >= 0]
            hit = next((i for i, m in enumerate(ms) if m.id == job_id), None)
            if hit is None or len(ms) < 2:
                margins[label] = None
                continue
            rival = ms[1].score if hit == 0 else ms[0].score
            margins[label] = ms[hit].score - rival
        if margins.get("off") is None or margins.get("feedback") is None:
            continue
        seen += 1
        grew = margins["feedback"] > margins["off"]
        widened += grew
        rows.append([cid, f"{margins['off']:+.3f}", f"{margins['feedback']:+.3f}",
                     "wider" if grew else "narrower"])

    s.table(["candidate", "margin, congestion off", "margin, feedback load", "moved"],
            rows, widths=[11, 24, 23, 10])

    s.broke(
        "congestion control is NOT cold-start promotion",
        widened < seen,
        f"The new listing's lead widened for {widened} of {seen} candidates -- "
        f"it took {tally.get(job_id, 0)} slots in the round against the busiest "
        f"incumbent's {tally.most_common(1)[0][1]}, so it is being penalised too. "
        "A new job that matches a narrow niche saturates that niche within a few "
        "hundred requests and then looks exactly like an incumbent. Congestion "
        "control equalises load; it has no notion of age, and asking it to "
        "carry one is how a fairness term quietly becomes a growth hack that "
        "does neither job.",
    )
    s.note("""
        The actual fix for cold start is a separate, explicit term: a decaying
        boost on impressions-since-published, or an epsilon of exploration
        traffic reserved for under-served listings. Separate, because it needs
        its own decay, its own budget, and its own defence against the obvious
        exploit -- delete and repost to reset your age. That is why the loader
        fingerprints posting text and counts reposts rather than trusting the
        posting id (ingest/load.py:dedupe_jobs).
    """)

    # -----------------------------------------------------------------------
    s.section("The other cold start: a candidate with almost no resume")
    s.note("""
        The symmetric case is worse, and it is the one people forget. A thin
        resume produces a vector near the centroid of the corpus: weakly
        similar to everything, strongly similar to nothing. It is never
        anyone's top match and always in everyone's pool.
    """)

    # The detailed profile must NOT be built from the job's own text. Doing
    # that measures self-similarity and reports ~0.98, which proves only that
    # a document matches itself -- the exact leakage the synthetic candidate
    # generator is written to avoid (see ingest/synth.py). Use a real resume
    # from the corpus instead: candidate 4, whose wording is independent of
    # every posting.
    thin = embedder.encode_fields([{"title": "Engineer", "skills": "python",
                                    "body": "Software engineer looking for work."}])[0]
    rich_row = conn.execute(
        "SELECT concat_ws(' ', c.headline, c.current_title), r.parsed->>'skills', r.raw_text "
        "FROM resumes r JOIN candidates c ON c.id = r.candidate_id WHERE r.candidate_id = 4"
    ).fetchone()
    rich = embedder.encode_fields([{"title": rich_row[0], "skills": rich_row[1],
                                    "body": rich_row[2]}])[0]

    thin_top = conn.execute(
        "SELECT max(1 - (embedding <=> %s::vector)) FROM job_docs WHERE embedding IS NOT NULL",
        (to_pgvector(thin),),
    ).fetchone()[0]
    rich_top = conn.execute(
        "SELECT max(1 - (embedding <=> %s::vector)) FROM job_docs WHERE embedding IS NOT NULL",
        (to_pgvector(rich),),
    ).fetchone()[0]

    s.fact("best cosine for a one-line resume", f"{float(thin_top):.3f}")
    s.fact("best cosine for a full resume (candidate 4)", f"{float(rich_top):.3f}")
    s.broke(
        "a thin profile cannot be matched well by any amount of ranking",
        float(thin_top) < float(rich_top) * 0.75,
        f"{float(thin_top):.3f} against {float(rich_top):.3f}. There is no "
        "modelling fix for missing input. The fix is a product one -- ask for "
        "skills at signup -- which is why onboarding forms exist and why "
        "'just upload your CV' is a ranking decision disguised as a UX one.",
    )

    # Leave the database as we found it.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE source = 'synthetic' AND source_id = 'coldstart-1'")
        cur.execute("DELETE FROM employers WHERE name = 'Coldstart Data Co'")
    conn.commit()

exit_with(s)
