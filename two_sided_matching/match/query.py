"""Database side of matching: retrieve a pool, then run the engine over it.

The split matters. SQL does what SQL is good at -- getting the 200 nearest
vectors out of two thousand rows quickly. Python does the ranking, because the
ranking is where the ideas are and you want to be able to read it.

At two thousand rows that split is obviously fine. It stays fine much longer
than people expect: the pool is a fixed 200 rows no matter how large the corpus
gets, so the Python half is O(pool), not O(corpus). What eventually forces
ranking into the database is not corpus size but wanting the filter to run
before the ANN search -- see scenarios/07.
"""

from __future__ import annotations

import time

from match.engine import (
    Match,
    Options,
    Result,
    candidate_wants,
    congestion_penalty,
    eligibility,
    employer_wants,
    harmonic,
    rerank_score,
    _normalise,
)

# Columns every scoring function expects, in one place so the two directions
# cannot drift apart. They did, once, and the bug looked like "reciprocal
# scoring is worse than cosine" rather than "job.parsed is missing here".
_JOB_COLUMNS = """
    j.id, j.title, j.status, j.location, j.country, j.remote, j.employment,
    j.salary_min, j.salary_max, j.currency, j.min_years_exp, j.seniority,
    j.sponsors_visa, j.headcount, j.source, j.source_url,
    e.name AS company, e.company_size, d.parsed
"""

_CAND_COLUMNS = """
    c.id, c.full_name, c.headline, c.years_exp, c.seniority, c.current_title,
    c.location, c.country, p.min_salary, p.remote_required,
    p.willing_locations, p.needs_visa, p.min_company_size, p.open_to_contract,
    r.parsed
"""


def _job_row(row) -> dict:
    (jid, title, status, location, country, remote, employment, smin, smax,
     currency, min_years, seniority, sponsors, headcount, source, url,
     company, size, parsed) = row
    return {
        "id": jid, "title": title, "status": status, "location": location,
        "country": country, "remote": remote, "employment": employment,
        "salary_min": smin, "salary_max": smax, "currency": currency,
        "min_years_exp": float(min_years) if min_years is not None else None,
        "seniority": seniority, "sponsors_visa": sponsors, "headcount": headcount,
        "source": source, "source_url": url, "company": company,
        "company_size": size, "parsed": parsed or {},
    }


def _cand_row(row) -> dict:
    (cid, name, headline, years, seniority, current, location, country,
     min_salary, remote_req, willing, needs_visa, min_size, contract, parsed) = row
    parsed = parsed or {}
    return {
        "id": cid, "full_name": name, "headline": headline,
        "years_exp": float(years) if years is not None else None,
        "seniority": seniority, "current_title": current, "location": location,
        "country": country, "min_salary": min_salary,
        "remote_required": remote_req, "willing_locations": list(willing or []),
        "needs_visa": needs_visa, "min_company_size": min_size,
        "open_to_contract": contract, "skills": parsed.get("skills") or [],
        "parsed": parsed,
    }


def load_candidate(conn, candidate_id: int) -> dict:
    row = conn.execute(
        f"""
        SELECT {_CAND_COLUMNS}
        FROM candidates c
        JOIN candidate_prefs p ON p.candidate_id = c.id
        JOIN resumes r         ON r.candidate_id = c.id
        WHERE c.id = %s
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"No candidate {candidate_id}. Try: ./lab.sh candidates")
    return _cand_row(row)


def load_job(conn, job_id: int) -> dict:
    row = conn.execute(
        f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs j
        JOIN employers e ON e.id = j.employer_id
        JOIN job_docs d  ON d.job_id = j.id
        WHERE j.id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"No job {job_id}. Try: ./lab.sh jobs")
    return _job_row(row)


def _applicant_counts(conn, job_ids: list[int]) -> dict[int, int]:
    if not job_ids:
        return {}
    rows = conn.execute(
        """
        SELECT job_id, count(*) FROM interactions
        WHERE job_id = ANY(%s) AND kind IN ('apply', 'save')
        GROUP BY job_id
        """,
        (job_ids,),
    ).fetchall()
    return {jid: n for jid, n in rows}


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _retrieve_jobs_hybrid(conn, vec_sql: str, params: tuple, opts: Options, skills: list[str]):
    """Vector pool UNION lexical pool, both scored by vector distance.

    Pure vector retrieval puts a hard ceiling on everything downstream: the
    reranker can only reorder what it is handed, so any job the embedding ranks
    at position 1,032 is invisible no matter how good the reranker is or how
    deep the pool goes. scenarios/05 measures exactly that ceiling.

    The fix is not a bigger pool -- it is a second retrieval channel that sees
    the signal the vector misses. Here that signal is exact skill overlap: the
    embedding blurs "airflow" into a 384-dimensional projection shared with
    thousands of other terms, while a set intersection does not blur at all.
    Retrieve on both and union. This is what "hybrid search" means, and the
    reason it is standard is that the two channels fail on different documents.

    The lexical half is a sequential scan here. At 1,865 rows that costs about
    a millisecond; the index that makes it scale is a GIN over the skills array
    (see schema/01_schema.sql), which is worth adding before this table has
    six figures in it.
    """
    half = max(10, opts.pool // 2)
    return conn.execute(
        f"""
        WITH vec AS (
            SELECT d.job_id
            FROM job_docs d
            WHERE d.embedding IS NOT NULL
            ORDER BY d.embedding <=> {vec_sql}
            LIMIT %s
        ),
        lex AS (
            SELECT d.job_id,
                   (SELECT count(*)
                      FROM jsonb_array_elements_text(d.parsed->'skills') sk
                     WHERE sk = ANY(%s)) AS overlap
            FROM job_docs d
            WHERE d.embedding IS NOT NULL
              AND d.parsed->'skills' ?| %s
            ORDER BY overlap DESC
            LIMIT %s
        ),
        pool AS (
            SELECT job_id FROM vec
            UNION
            SELECT job_id FROM lex
        )
        SELECT {_JOB_COLUMNS}, (d.embedding <=> {vec_sql}) AS dist
        FROM pool
        JOIN job_docs d  ON d.job_id = pool.job_id
        JOIN jobs j      ON j.id = d.job_id
        JOIN employers e ON e.id = j.employer_id
        ORDER BY dist
        """,
        params + (half, skills, skills, half) + params,
    ).fetchall()


def _retrieve_jobs(conn, vec_sql: str, params: tuple, opts: Options):
    # enable_indexscan is toggled rather than the query rewritten, so `exact`
    # and `ann` run the SAME statement. Comparing two different queries and
    # attributing the difference to the index is how people conclude their ANN
    # index made things slower when they had also changed the join order.
    if opts.retrieval == "exact":
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
    return conn.execute(
        f"""
        SELECT {_JOB_COLUMNS}, (d.embedding <=> {vec_sql}) AS dist
        FROM job_docs d
        JOIN jobs j      ON j.id = d.job_id
        JOIN employers e ON e.id = j.employer_id
        WHERE d.embedding IS NOT NULL
        ORDER BY d.embedding <=> {vec_sql}
        LIMIT %s
        """,
        params + params + (opts.pool,),
    ).fetchall()


def _retrieve_candidates(conn, vec_sql: str, params: tuple, opts: Options):
    if opts.retrieval == "exact":
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
    return conn.execute(
        f"""
        SELECT {_CAND_COLUMNS}, (r.embedding <=> {vec_sql}) AS dist
        FROM resumes r
        JOIN candidates c      ON c.id = r.candidate_id
        JOIN candidate_prefs p ON p.candidate_id = c.id
        WHERE r.embedding IS NOT NULL
        ORDER BY r.embedding <=> {vec_sql}
        LIMIT %s
        """,
        params + params + (opts.pool,),
    ).fetchall()


# ---------------------------------------------------------------------------
# The two public entry points
# ---------------------------------------------------------------------------

def suggest_jobs(conn, candidate_id: int, opts: Options | None = None) -> Result:
    opts = opts or Options()
    timings: dict[str, float] = {}
    cand = load_candidate(conn, candidate_id)

    t0 = time.perf_counter()
    vec_sql = "(SELECT embedding FROM resumes WHERE candidate_id = %s)"
    if opts.retrieval == "hybrid":
        rows = _retrieve_jobs_hybrid(conn, vec_sql, (candidate_id,), opts,
                                     cand.get("skills") or [""])
    else:
        rows = _retrieve_jobs(conn, vec_sql, (candidate_id,), opts)
    timings["retrieve"] = (time.perf_counter() - t0) * 1000

    jobs = [_job_row(r[:-1]) for r in rows]
    sims = [1.0 - float(r[-1]) for r in rows]     # <=> is cosine DISTANCE

    if not opts.congestion:
        loads = {}
    elif opts.serving_load is not None:
        loads = opts.serving_load
    else:
        loads = _applicant_counts(conn, [j["id"] for j in jobs])

    t0 = time.perf_counter()
    matches = _score(cand, jobs, sims, opts, loads, direction="jobs")
    timings["score"] = (time.perf_counter() - t0) * 1000

    eligible = len(matches)
    if opts.rerank:
        t0 = time.perf_counter()
        matches = _apply_rerank(cand, matches, opts)
        timings["rerank"] = (time.perf_counter() - t0) * 1000

    matches.sort(key=lambda m: -m.score)
    return Result(matches[: opts.limit], len(jobs), eligible, timings)


def suggest_candidates(conn, job_id: int, opts: Options | None = None) -> Result:
    """The mirror image. Same engine, swapped roles.

    Note what does NOT change: eligibility() is called with the same argument
    order and returns the same verdict for the same pair. Note what does: the
    population being ranked, and which directional score the results are
    sorted by when scoring="cosine" is replaced by something two-sided.
    """
    opts = opts or Options()
    timings: dict[str, float] = {}
    job = load_job(conn, job_id)

    t0 = time.perf_counter()
    rows = _retrieve_candidates(
        conn,
        "(SELECT embedding FROM job_docs WHERE job_id = %s)",
        (job_id,),
        opts,
    )
    timings["retrieve"] = (time.perf_counter() - t0) * 1000

    cands = [_cand_row(r[:-1]) for r in rows]
    sims = [1.0 - float(r[-1]) for r in rows]

    t0 = time.perf_counter()
    matches = _score_candidates(job, cands, sims, opts)
    timings["score"] = (time.perf_counter() - t0) * 1000

    eligible = len(matches)
    if opts.rerank:
        t0 = time.perf_counter()
        for m in matches:
            bonus, why = rerank_score(m.meta["cand"], job)
            m.score = 0.5 * m.score + 0.5 * bonus
            if opts.explain:
                m.reasons.extend(why)
        timings["rerank"] = (time.perf_counter() - t0) * 1000

    matches.sort(key=lambda m: -m.score)
    return Result(matches[: opts.limit], len(cands), eligible, timings)


# ---------------------------------------------------------------------------
# Shared scoring
# ---------------------------------------------------------------------------

def _score(cand, jobs, sims, opts, loads, *, direction) -> list[Match]:
    keep: list[tuple[dict, float]] = []
    rejected: list[Match] = []

    for job, sim in zip(jobs, sims):
        blockers = eligibility(cand, job, opts.filters)
        if blockers:
            if opts.explain:
                rejected.append(Match(
                    id=job["id"], title=job["title"],
                    subtitle=job.get("company") or "", sim=sim, score=-1.0,
                    blockers=blockers, meta={"job": job},
                ))
            continue
        keep.append((job, sim))

    if not keep:
        # Returning the rejects, clearly marked, beats returning nothing. "No
        # results" tells the user their profile is broken; "12 jobs matched
        # your skills but all pay below your floor of 400,000" tells them which
        # knob to turn.
        return rejected[: opts.limit] if opts.explain else []

    sim_norm = _normalise([s for _, s in keep])
    out: list[Match] = []

    for (job, sim), sn in zip(keep, sim_norm):
        want_c, why_c = candidate_wants(cand, job)
        want_e, why_e = employer_wants(cand, job)

        if opts.scoring == "cosine":
            # The naive system: one symmetric number, both directions equal.
            s_a2b = s_b2a = sim
            score = sim
        else:
            s_a2b = 0.5 * sn + 0.5 * want_c      # candidate -> job
            s_b2a = 0.5 * sn + 0.5 * want_e      # job -> candidate
            score = harmonic(s_a2b, s_b2a)

        if opts.congestion:
            load = loads.get(job["id"], 0)
            penalty = congestion_penalty(load, job.get("headcount") or 1, opts.congestion)
            score *= penalty
            if opts.explain and load:
                why_c.append(f"{load} applicants (x{penalty:.2f})")

        out.append(Match(
            id=job["id"], title=job["title"], subtitle=job.get("company") or "",
            sim=sim, score=score, s_a2b=s_a2b, s_b2a=s_b2a,
            reasons=(why_c + why_e) if opts.explain else [],
            meta={"job": job},
        ))
    return out


def _score_candidates(job, cands, sims, opts) -> list[Match]:
    keep = []
    rejected: list[Match] = []
    for cand, sim in zip(cands, sims):
        blockers = eligibility(cand, job, opts.filters)
        if blockers:
            if opts.explain:
                rejected.append(Match(
                    id=cand["id"], title=cand["full_name"],
                    subtitle=cand.get("headline") or "", sim=sim, score=-1.0,
                    blockers=blockers, meta={"cand": cand},
                ))
            continue
        keep.append((cand, sim))

    if not keep:
        return rejected[: opts.limit] if opts.explain else []

    sim_norm = _normalise([s for _, s in keep])
    out = []
    for (cand, sim), sn in zip(keep, sim_norm):
        want_c, why_c = candidate_wants(cand, job)
        want_e, why_e = employer_wants(cand, job)

        if opts.scoring == "cosine":
            s_a2b = s_b2a = sim
            score = sim
        else:
            # a2b is still "the asking side wants this": here the asker is the
            # employer, so a2b is employer_wants. Getting this backwards gives
            # you a list sorted by how much the CANDIDATE would enjoy the job,
            # shown to the employer, and it looks plausible enough to ship.
            s_a2b = 0.5 * sn + 0.5 * want_e
            s_b2a = 0.5 * sn + 0.5 * want_c
            score = harmonic(s_a2b, s_b2a)

        out.append(Match(
            id=cand["id"], title=cand["full_name"],
            subtitle=cand.get("headline") or "", sim=sim, score=score,
            s_a2b=s_a2b, s_b2a=s_b2a,
            reasons=(why_e + why_c) if opts.explain else [],
            meta={"cand": cand},
        ))
    return out


def _apply_rerank(cand, matches: list[Match], opts: Options) -> list[Match]:
    for m in matches:
        job = m.meta.get("job")
        if job is None:
            continue
        bonus, why = rerank_score(cand, job)
        # Blend rather than replace. The retrieval score carries information
        # the reranker does not see (the whole document, both preference
        # models); throwing it away is a common and expensive mistake.
        m.score = 0.5 * m.score + 0.5 * bonus
        if opts.explain:
            m.reasons.extend(why)
    return matches
