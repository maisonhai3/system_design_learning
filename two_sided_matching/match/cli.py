# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "numpy"]
# ///
"""Run the matcher from the command line, both directions.

    ./lab.sh suggest-jobs 4
    ./lab.sh suggest-jobs 4 --scoring cosine --filters off
    ./lab.sh suggest-candidates 812 --rerank --congestion 1.0
    ./lab.sh candidates --focus data
    ./lab.sh jobs --q kubernetes
    ./lab.sh doctor
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lab import db  # noqa: E402
from match.engine import Options  # noqa: E402
from match.query import load_candidate, load_job, suggest_candidates, suggest_jobs  # noqa: E402

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
)


def _fmt_salary(lo, hi, cur="USD") -> str:
    if lo and hi:
        return f"{lo // 1000}-{hi // 1000}k {cur}"
    if hi:
        return f"<={hi // 1000}k {cur}"
    if lo:
        return f">={lo // 1000}k {cur}"
    return "—"


def _print_result(res, *, kind: str, opts: Options) -> None:
    head = (
        f"pool {res.pool_size}  eligible {res.eligible}  shown {len(res.matches)}   "
        f"[scoring={opts.scoring} filters={opts.filters}"
        + (f" congestion={opts.congestion}" if opts.congestion else "")
        + (" rerank" if opts.rerank else "")
        + "]"
    )
    print(f"{DIM}{head}{RESET}")
    timing = "  ".join(f"{k} {v:.1f}ms" for k, v in res.timings_ms.items())
    print(f"{DIM}{timing}{RESET}\n")

    if not res.matches:
        print(f"{RED}Nothing eligible.{RESET} Try --filters lenient, or --filters off.")
        return

    if res.matches[0].score < 0:
        print(f"{RED}Nothing eligible — showing why the closest matches were rejected:{RESET}\n")
        for m in res.matches:
            print(f"  {m.title[:52]:52s} {DIM}{m.subtitle[:26]}{RESET}")
            for b in m.blockers:
                print(f"      {RED}x{RESET} {b}")
        return

    for rank, m in enumerate(res.matches, 1):
        bar = "#" * int(round(m.score * 24))
        print(f"{BOLD}{rank:2d}.{RESET} {m.title[:54]:54s} {GREEN}{m.score:.3f}{RESET} {DIM}{bar}{RESET}")
        detail = f"    {m.subtitle[:36]:36s}"
        if kind == "jobs":
            job = m.meta["job"]
            detail += (
                f"  {_fmt_salary(job['salary_min'], job['salary_max'], job['currency'])}"
                f"  {'remote' if job['remote'] else (job['country'] or '?')}"
                f"  {DIM}#{m.id}{RESET}"
            )
        else:
            cand = m.meta["cand"]
            detail += (
                f"  {cand['years_exp'] or '?'}y  {cand['seniority'] or '?'}"
                f"  {cand['country'] or '?'}  {DIM}#{m.id}{RESET}"
            )
        print(detail)
        # The two directional scores side by side are the whole lesson: when
        # they disagree, the harmonic mean is why the row is where it is.
        print(f"    {DIM}cos {m.sim:.3f}   wants→ {m.s_a2b:.2f}   ←wanted {m.s_b2a:.2f}{RESET}")
        if m.reasons:
            print(f"    {YELLOW}{' · '.join(m.reasons[:5])}{RESET}")
        print()


def _add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--pool", type=int, default=200)
    ap.add_argument("--filters", choices=["strict", "lenient", "off"], default="lenient")
    ap.add_argument("--scoring", choices=["cosine", "reciprocal"], default="reciprocal")
    ap.add_argument("--congestion", type=float, default=0.0)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--retrieval", choices=["exact", "ann", "hybrid"], default="exact")


def _opts(args) -> Options:
    return Options(
        limit=args.limit, pool=args.pool, filters=args.filters,
        scoring=args.scoring, congestion=args.congestion,
        rerank=args.rerank, retrieval=args.retrieval,
    )


def cmd_suggest_jobs(args) -> int:
    with db.connect() as conn:
        db.require_embeddings(conn)
        cand = load_candidate(conn, args.id)
        print(f"\n{BOLD}{cand['full_name']}{RESET} — {cand['headline']}")
        print(f"{DIM}{cand['years_exp']}y {cand['seniority']}  {cand['location']} "
              f"({cand['country']})  floor {cand['min_salary'] or '—'}  "
              f"{'remote-only' if cand['remote_required'] else 'open to onsite'}"
              f"{'  needs visa' if cand['needs_visa'] else ''}{RESET}")
        print(f"{DIM}skills: {', '.join(cand['skills'][:12])}{RESET}\n")
        res = suggest_jobs(conn, args.id, _opts(args))
    _print_result(res, kind="jobs", opts=_opts(args))
    return 0


def cmd_suggest_candidates(args) -> int:
    with db.connect() as conn:
        db.require_embeddings(conn)
        job = load_job(conn, args.id)
        print(f"\n{BOLD}{job['title']}{RESET} — {job['company']}")
        print(f"{DIM}{_fmt_salary(job['salary_min'], job['salary_max'], job['currency'])}  "
              f"{'remote' if job['remote'] else 'onsite'}  {job['country'] or '?'}  "
              f"needs {job['min_years_exp'] or '—'}y  {job['seniority'] or '—'}"
              f"{'  sponsors visa' if job['sponsors_visa'] else ''}{RESET}")
        print(f"{DIM}skills: {', '.join((job['parsed'].get('skills') or [])[:12])}{RESET}\n")
        res = suggest_candidates(conn, args.id, _opts(args))
    _print_result(res, kind="candidates", opts=_opts(args))
    return 0


def cmd_candidates(args) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.full_name, c.headline, c.years_exp, c.country,
                   p.min_salary, p.remote_required, r.parsed->>'focus'
            FROM candidates c
            JOIN candidate_prefs p ON p.candidate_id = c.id
            JOIN resumes r ON r.candidate_id = c.id
            WHERE (%s = '' OR r.parsed->>'focus' = %s)
              AND (%s = '' OR c.full_name ILIKE '%%' || %s || '%%')
            ORDER BY c.id LIMIT %s
            """,
            (args.focus, args.focus, args.q, args.q, args.limit),
        ).fetchall()
    print(f"{DIM}{'id':>5}  {'name':22s} {'focus':10s} {'yrs':>4} {'loc':4s} {'floor':>8}  headline{RESET}")
    for cid, name, headline, yrs, country, floor, remote, focus in rows:
        flag = "R" if remote else " "
        print(f"{cid:>5}  {name[:22]:22s} {(focus or '-'):10s} {yrs or 0:>4} "
              f"{(country or '-'):4s} {floor or 0:>8} {flag} {DIM}{(headline or '')[:40]}{RESET}")
    return 0


def cmd_jobs(args) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.title, e.name, j.country, j.remote,
                   j.salary_min, j.salary_max, j.min_years_exp, j.source
            FROM jobs j
            JOIN employers e ON e.id = j.employer_id
            JOIN job_docs d ON d.job_id = j.id
            WHERE j.status = 'open'
              AND (%s = '' OR j.title ILIKE '%%' || %s || '%%'
                           OR d.raw_text ILIKE '%%' || %s || '%%')
            ORDER BY j.id LIMIT %s
            """,
            (args.q, args.q, args.q, args.limit),
        ).fetchall()
    print(f"{DIM}{'id':>5}  {'title':44s} {'company':22s} {'loc':4s} {'pay':>14}{RESET}")
    for jid, title, comp, country, remote, lo, hi, yrs, source in rows:
        loc = "REM" if remote else (country or "-")
        print(f"{jid:>5}  {title[:44]:44s} {(comp or '')[:22]:22s} {loc:4s} "
              f"{_fmt_salary(lo, hi):>14}")
    return 0


def cmd_doctor(args) -> int:
    """Field coverage. The most useful five seconds in the whole lab.

    Every hard filter is a bet that a column is populated. These percentages
    are what the bet actually pays, and they are much lower than anyone
    designing a filter in the abstract assumes.
    """
    with db.connect() as conn:
        counts = db.table_counts(conn)
        total = counts["jobs"] or 1
        cov = conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE salary_max IS NOT NULL),
              count(*) FILTER (WHERE country IS NOT NULL),
              count(*) FILTER (WHERE remote),
              count(*) FILTER (WHERE min_years_exp IS NOT NULL),
              count(*) FILTER (WHERE seniority IS NOT NULL),
              count(*) FILTER (WHERE sponsors_visa),
              count(*) FILTER (WHERE employment IS NOT NULL)
            FROM jobs
            """
        ).fetchone()
        inferred = conn.execute(
            "SELECT count(*) FROM job_docs WHERE (parsed->>'years_inferred')::boolean"
        ).fetchone()[0]
        vecs = conn.execute(
            "SELECT count(*) FILTER (WHERE embedding IS NOT NULL), "
            "       count(DISTINCT embedding_model) FROM job_docs"
        ).fetchone()

    print(f"\n{BOLD}rows{RESET}")
    for table, n in counts.items():
        print(f"  {table:14s} {n:6d}")

    print(f"\n{BOLD}job field coverage{RESET}  {DIM}(every hard filter is a bet on one of these){RESET}")
    labels = ["salary_max", "country", "remote=true", "min_years_exp",
              "seniority", "sponsors_visa", "employment"]
    for label, n in zip(labels, cov):
        pct = 100 * n / total
        colour = RED if pct < 25 else (YELLOW if pct < 60 else GREEN)
        bar = "#" * int(pct / 4)
        print(f"  {label:16s} {colour}{n:5d}  {pct:5.1f}%{RESET} {DIM}{bar}{RESET}")
    print(f"  {DIM}of which min_years_exp inferred from a title word, not stated: {inferred}{RESET}")

    print(f"\n{BOLD}vectors{RESET}")
    print(f"  embedded {vecs[0]}/{total}   distinct models {vecs[1]}")
    if vecs[1] and vecs[1] > 1:
        print(f"  {RED}More than one embedding model in the table.{RESET} "
              "Vectors from different models are not comparable — run ./lab.sh embed --force.")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="lab", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("suggest-jobs"); p.add_argument("id", type=int)
    _add_common(p); p.set_defaults(fn=cmd_suggest_jobs)

    p = sub.add_parser("suggest-candidates"); p.add_argument("id", type=int)
    _add_common(p); p.set_defaults(fn=cmd_suggest_candidates)

    p = sub.add_parser("candidates")
    p.add_argument("--focus", default=""); p.add_argument("--q", default="")
    p.add_argument("--limit", type=int, default=25); p.set_defaults(fn=cmd_candidates)

    p = sub.add_parser("jobs")
    p.add_argument("--q", default=""); p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_jobs)

    p = sub.add_parser("doctor"); p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
