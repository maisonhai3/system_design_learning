# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""Load fixtures into Postgres: dedupe, upsert, and seed interactions.

    ./lab.sh load                 # everything from fixtures/
    ./lab.sh load --truncate      # wipe first
    ./lab.sh load --no-interactions

Embedding is a separate step (`./lab.sh embed`) on purpose. Loading is cheap
and idempotent; embedding is the expensive part you want to re-run on its own
when you change the model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from lab import db  # noqa: E402
from lab.text import extract_skills  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _shingle_hash(text: str, size: int = 400) -> str:
    """Fingerprint of the first `size` normalised characters.

    Cheap near-duplicate detection, and it is enough for what this corpus
    actually contains. Companies repost the identical listing to Hacker News
    every month -- 228 of them in a five-month pull -- usually editing nothing
    but the closing line. Comparing a normalised prefix catches those without
    the machinery of MinHash, and the failure mode (two genuinely different
    jobs sharing a 400-character boilerplate header) is rare enough to accept
    in a lab and worth upgrading in anything real.
    """
    normalised = _WS.sub(" ", text.lower()).strip()[:size]
    return hashlib.blake2b(normalised.encode(), digest_size=12).hexdigest()


def dedupe_jobs(records: list[dict]) -> tuple[list[dict], int]:
    """Collapse reposts, keeping the newest and counting the repeats.

    The repost count is kept rather than thrown away because it is a real
    signal: a job posted five months running is one that is not getting
    filled. A recommender that treats the fifth posting as a brand new
    opportunity keeps showing candidates a vacancy the market has already
    declined, and it does it five times.
    """
    best: dict[str, dict] = {}
    for rec in records:
        key = _shingle_hash(rec["raw_text"])
        seen = best.get(key)
        if seen is None:
            rec["repost_count"] = 1
            best[key] = rec
            continue
        seen["repost_count"] += 1
        # Keep whichever posting is newest; posted_at is None for some sources,
        # and None must not win a max() against a real timestamp.
        if (rec.get("posted_at") or "") > (seen.get("posted_at") or ""):
            rec["repost_count"] = seen["repost_count"]
            best[key] = rec
    kept = list(best.values())
    return kept, len(records) - len(kept)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_jobs(conn, records: list[dict]) -> int:
    employers: dict[str, int] = {}
    inserted = 0

    with conn.cursor() as cur:
        for i, rec in enumerate(records, 1):
            name = (rec.get("company") or "Unknown").strip()[:200] or "Unknown"
            employer_id = employers.get(name)
            if employer_id is None:
                # ON CONFLICT DO UPDATE, not DO NOTHING: DO NOTHING returns no
                # row, so RETURNING gives you None on the second run and the
                # whole load fails the moment it is not the first run.
                employer_id = cur.execute(
                    """
                    INSERT INTO employers (name, company_size)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                    """,
                    (name, rec.get("company_size")),
                ).fetchone()[0]
                employers[name] = employer_id

            job_id = cur.execute(
                """
                INSERT INTO jobs (
                    employer_id, title, status, location, country, remote,
                    employment, salary_min, salary_max, currency,
                    min_years_exp, seniority, sponsors_visa, headcount,
                    source, source_id, source_url, posted_at
                )
                VALUES (%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source, source_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    salary_min = EXCLUDED.salary_min,
                    salary_max = EXCLUDED.salary_max,
                    remote = EXCLUDED.remote
                RETURNING id
                """,
                (
                    employer_id, rec["title"][:200], rec.get("location"),
                    rec.get("country"), bool(rec.get("remote")),
                    rec.get("employment"), rec.get("salary_min"), rec.get("salary_max"),
                    rec.get("currency") or "USD", rec.get("min_years_exp"),
                    rec.get("seniority"), bool(rec.get("sponsors_visa")),
                    max(1, int(rec.get("headcount") or 1)),
                    rec["source"], rec["source_id"], rec.get("source_url"),
                    rec.get("posted_at"),
                ),
            ).fetchone()[0]

            parsed = {
                "skills": rec.get("skills") or [],
                "tags": rec.get("tags") or [],
                "repost_count": rec.get("repost_count", 1),
                # Recorded so scenarios/02 can separate a requirement the
                # posting actually stated from one this pipeline guessed at.
                "years_inferred": bool(rec.get("years_inferred")),
            }
            cur.execute(
                """
                INSERT INTO job_docs (job_id, raw_text, parsed)
                VALUES (%s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    raw_text = EXCLUDED.raw_text,
                    parsed   = EXCLUDED.parsed,
                    -- The text changed, so the stored vector no longer
                    -- describes it. Clearing these is what makes `./lab.sh
                    -- embed` pick the row up again; leaving a stale vector in
                    -- place is the bug where search results quietly describe
                    -- last month's posting.
                    embedding = NULL,
                    content_hash = NULL,
                    embedded_at = NULL,
                    updated_at = now()
                WHERE job_docs.raw_text IS DISTINCT FROM EXCLUDED.raw_text
                """,
                (job_id, rec["raw_text"], json.dumps(parsed)),
            )
            inserted += 1
            db.progress("jobs", i, len(records))
    return inserted


def load_candidates(conn, records: list[dict]) -> int:
    with conn.cursor() as cur:
        for i, rec in enumerate(records, 1):
            cand_id = cur.execute(
                """
                INSERT INTO candidates (
                    full_name, email, headline, years_exp, seniority,
                    current_title, location, country
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (email) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    headline  = EXCLUDED.headline,
                    years_exp = EXCLUDED.years_exp
                RETURNING id
                """,
                (
                    rec["full_name"], rec["email"], rec.get("headline"),
                    rec.get("years_exp"), rec.get("seniority"),
                    rec.get("current_title"), rec.get("location"), rec.get("country"),
                ),
            ).fetchone()[0]

            prefs = rec.get("prefs") or {}
            cur.execute(
                """
                INSERT INTO candidate_prefs (
                    candidate_id, min_salary, remote_required, willing_locations,
                    needs_visa, min_company_size, open_to_contract
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (candidate_id) DO UPDATE SET
                    min_salary        = EXCLUDED.min_salary,
                    remote_required   = EXCLUDED.remote_required,
                    willing_locations = EXCLUDED.willing_locations,
                    needs_visa        = EXCLUDED.needs_visa,
                    min_company_size  = EXCLUDED.min_company_size,
                    open_to_contract  = EXCLUDED.open_to_contract
                """,
                (
                    cand_id, prefs.get("min_salary"), bool(prefs.get("remote_required")),
                    prefs.get("willing_locations") or [], bool(prefs.get("needs_visa")),
                    prefs.get("min_company_size"), bool(prefs.get("open_to_contract", True)),
                ),
            )

            parsed = {
                "skills": rec.get("skills") or extract_skills(rec["raw_text"]),
                "focus": rec.get("focus"),
                "external_id": rec.get("external_id"),
            }
            cur.execute(
                """
                INSERT INTO resumes (candidate_id, raw_text, parsed)
                VALUES (%s, %s, %s)
                ON CONFLICT (candidate_id) DO UPDATE SET
                    raw_text = EXCLUDED.raw_text,
                    parsed   = EXCLUDED.parsed,
                    embedding = NULL,
                    content_hash = NULL,
                    embedded_at = NULL,
                    updated_at = now()
                WHERE resumes.raw_text IS DISTINCT FROM EXCLUDED.raw_text
                """,
                (cand_id, rec["raw_text"], json.dumps(parsed)),
            )
            db.progress("candidates", i, len(records))
    return len(records)


def seed_interactions(conn, seed: int = 20260909) -> int:
    """Invent a plausible application history.

    Deliberately POPULARITY-BIASED rather than uniform. Real application
    traffic follows attention, and attention follows whatever was recommended
    yesterday -- so applications pile onto a small number of well-known
    postings. Seeding uniformly would hand the congestion scenario a corpus
    that is already fair, and it would show a rich-get-richer effect that the
    generator, not the ranker, had ruled out in advance.
    """
    rng = random.Random(seed)
    with conn.cursor() as cur:
        cand_ids = [r[0] for r in cur.execute("SELECT id FROM candidates ORDER BY id").fetchall()]
        job_rows = cur.execute(
            "SELECT id FROM jobs WHERE status = 'open' ORDER BY id"
        ).fetchall()
        job_ids = [r[0] for r in job_rows]
        if not cand_ids or not job_ids:
            return 0

        cur.execute("TRUNCATE interactions")

        # Zipf-ish popularity: rank r gets weight 1/(r+bias).
        order = job_ids[:]
        rng.shuffle(order)
        weights = [1.0 / (i + 12) for i in range(len(order))]
        total = sum(weights)
        weights = [w / total for w in weights]

        rows = []
        for cand_id in cand_ids:
            for _ in range(rng.randint(0, 9)):
                job_id = rng.choices(order, weights=weights, k=1)[0]
                kind = rng.choices(
                    ["impression", "click", "save", "apply"],
                    weights=[0.55, 0.25, 0.12, 0.08], k=1,
                )[0]
                rows.append((cand_id, job_id, kind, "candidate"))
        cur.executemany(
            "INSERT INTO interactions (candidate_id, job_id, kind, actor) VALUES (%s,%s,%s,%s)",
            rows,
        )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jobs", default=str(FIXTURES / "jobs.jsonl"))
    ap.add_argument("--candidates", default=str(FIXTURES / "candidates.jsonl"))
    ap.add_argument("--truncate", action="store_true", help="wipe tables first")
    ap.add_argument("--no-interactions", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="load only N of each (fast iteration)")
    args = ap.parse_args()

    job_path, cand_path = pathlib.Path(args.jobs), pathlib.Path(args.candidates)
    for path in (job_path, cand_path):
        if not path.exists():
            print(f"Missing {path}. Run ./lab.sh scrape && ./lab.sh synth", file=sys.stderr)
            return 1

    jobs = [json.loads(line) for line in job_path.open()]
    cands = [json.loads(line) for line in cand_path.open()]

    jobs, dropped = dedupe_jobs(jobs)
    print(f"jobs: {len(jobs)} unique ({dropped} reposts collapsed)")

    if args.limit:
        jobs, cands = jobs[: args.limit], cands[: args.limit]

    with db.connect() as conn:
        if args.truncate:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE interactions, resumes, job_docs, candidate_prefs, "
                    "jobs, candidates, employers RESTART IDENTITY CASCADE"
                )
            print("truncated")

        try:
            load_jobs(conn, jobs)
            load_candidates(conn, cands)
            n_inter = 0 if args.no_interactions else seed_interactions(conn)
            conn.commit()
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            print("Tables are missing. Run:  ./lab.sh up   (or ./lab.sh reset)", file=sys.stderr)
            return 1

        counts = db.table_counts(conn)

    print("\nloaded:")
    for table, n in counts.items():
        print(f"  {table:14s} {n:6d}")
    if not args.no_interactions:
        print(f"  (seeded {n_inter} interactions, popularity-biased)")
    print("\nNext: ./lab.sh embed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
