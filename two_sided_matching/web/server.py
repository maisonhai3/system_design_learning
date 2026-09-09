# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy", "fastapi", "uvicorn[standard]"]
# ///
"""A small web UI for poking at the matcher from both sides.

    ./lab.sh serve      ->  http://localhost:8000

Pick a candidate or a job, then flip the pipeline stages on and off and watch
the ranking move. Every knob here is the same Options object the CLI and the
scenarios use, so what you see is the real engine, not a demo of it.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from lab import db  # noqa: E402
from match.engine import Options  # noqa: E402
from match.query import (  # noqa: E402
    load_candidate,
    load_job,
    suggest_candidates,
    suggest_jobs,
)

HERE = pathlib.Path(__file__).resolve().parent
app = FastAPI(title="Two-Sided Matching Lab")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "index.html").read_text()


@app.get("/api/candidates")
def api_candidates(q: str = "", limit: int = 60):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.full_name, c.headline, c.years_exp, c.country,
                   p.min_salary, p.remote_required, r.parsed->>'focus'
            FROM candidates c
            JOIN candidate_prefs p ON p.candidate_id = c.id
            JOIN resumes r         ON r.candidate_id = c.id
            WHERE %s = '' OR c.full_name ILIKE '%%' || %s || '%%'
                          OR r.parsed->>'focus' ILIKE '%%' || %s || '%%'
            ORDER BY c.id LIMIT %s
            """,
            (q, q, q, limit),
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "headline": r[2],
         "years": float(r[3]) if r[3] is not None else None,
         "country": r[4], "floor": r[5], "remote": r[6], "focus": r[7]}
        for r in rows
    ]


@app.get("/api/jobs")
def api_jobs(q: str = "", limit: int = 60):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.title, e.name, j.country, j.remote,
                   j.salary_min, j.salary_max, j.seniority
            FROM jobs j
            JOIN employers e ON e.id = j.employer_id
            JOIN job_docs d  ON d.job_id = j.id
            WHERE j.status = 'open'
              AND (%s = '' OR j.title ILIKE '%%' || %s || '%%'
                           OR e.name ILIKE '%%' || %s || '%%'
                           OR d.raw_text ILIKE '%%' || %s || '%%')
            ORDER BY j.id LIMIT %s
            """,
            (q, q, q, q, limit),
        ).fetchall()
    return [
        {"id": r[0], "title": r[1], "company": r[2], "country": r[3],
         "remote": r[4], "salary_min": r[5], "salary_max": r[6], "seniority": r[7]}
        for r in rows
    ]


def _opts(limit, pool, filters, scoring, congestion, rerank, retrieval) -> Options:
    return Options(
        limit=limit, pool=pool, filters=filters, scoring=scoring,
        congestion=congestion, rerank=rerank, retrieval=retrieval, explain=True,
    )


def _serialise(res, kind: str) -> dict:
    out = []
    for m in res.matches:
        row = {
            "id": m.id, "title": m.title, "subtitle": m.subtitle,
            "sim": round(m.sim, 4), "score": round(m.score, 4),
            "a2b": round(m.s_a2b, 3), "b2a": round(m.s_b2a, 3),
            "reasons": m.reasons[:6], "blockers": m.blockers[:6],
        }
        if kind == "jobs":
            job = m.meta.get("job") or {}
            row |= {"salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
                    "currency": job.get("currency"), "remote": job.get("remote"),
                    "country": job.get("country"), "url": job.get("source_url"),
                    "source": job.get("source")}
        else:
            cand = m.meta.get("cand") or {}
            row |= {"years": cand.get("years_exp"), "seniority": cand.get("seniority"),
                    "country": cand.get("country"), "skills": (cand.get("skills") or [])[:8]}
        out.append(row)
    return {
        "matches": out,
        "pool": res.pool_size,
        "eligible": res.eligible,
        "timings": {k: round(v, 2) for k, v in res.timings_ms.items()},
        "empty_reason": bool(res.matches and res.matches[0].score < 0),
    }


@app.get("/api/suggest-jobs/{candidate_id}")
def api_suggest_jobs(
    candidate_id: int,
    limit: int = 15, pool: int = 300,
    filters: str = "lenient", scoring: str = "reciprocal",
    congestion: float = 0.0, rerank: bool = False, retrieval: str = "exact",
):
    with db.connect() as conn:
        cand = load_candidate(conn, candidate_id)
        res = suggest_jobs(conn, candidate_id, _opts(
            limit, pool, filters, scoring, congestion, rerank, retrieval))
    payload = _serialise(res, "jobs")
    payload["subject"] = {
        "id": cand["id"], "name": cand["full_name"], "headline": cand["headline"],
        "years": cand["years_exp"], "country": cand["country"],
        "floor": cand["min_salary"], "remote": cand["remote_required"],
        "visa": cand["needs_visa"], "locations": cand["willing_locations"],
        "skills": cand["skills"][:14],
    }
    return JSONResponse(payload)


@app.get("/api/suggest-candidates/{job_id}")
def api_suggest_candidates(
    job_id: int,
    limit: int = 15, pool: int = 300,
    filters: str = "lenient", scoring: str = "reciprocal",
    congestion: float = 0.0, rerank: bool = False, retrieval: str = "exact",
):
    with db.connect() as conn:
        job = load_job(conn, job_id)
        res = suggest_candidates(conn, job_id, _opts(
            limit, pool, filters, scoring, congestion, rerank, retrieval))
    payload = _serialise(res, "candidates")
    payload["subject"] = {
        "id": job["id"], "name": job["title"], "headline": job["company"],
        "years": job["min_years_exp"], "country": job["country"],
        "salary_min": job["salary_min"], "salary_max": job["salary_max"],
        "remote": job["remote"], "visa": job["sponsors_visa"],
        "skills": (job["parsed"].get("skills") or [])[:14],
        "url": job["source_url"],
    }
    return JSONResponse(payload)


@app.get("/api/stats")
def api_stats():
    with db.connect() as conn:
        counts = db.table_counts(conn)
        total = counts["jobs"] or 1
        cov = conn.execute(
            """
            SELECT count(*) FILTER (WHERE salary_max IS NOT NULL),
                   count(*) FILTER (WHERE country IS NOT NULL),
                   count(*) FILTER (WHERE remote),
                   count(*) FILTER (WHERE sponsors_visa)
            FROM jobs
            """
        ).fetchone()
        model = conn.execute(
            "SELECT embedding_model FROM job_docs WHERE embedding_model IS NOT NULL LIMIT 1"
        ).fetchone()
    return {
        "counts": counts,
        "model": model[0] if model else None,
        "coverage": {
            "salary_max": round(100 * cov[0] / total, 1),
            "country": round(100 * cov[1] / total, 1),
            "remote": round(100 * cov[2] / total, 1),
            "sponsors_visa": round(100 * cov[3] / total, 1),
        },
    }


if __name__ == "__main__":
    import argparse
    import os

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
