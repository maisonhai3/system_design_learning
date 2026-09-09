"""Connection helpers. One DSN, read from the environment, with a real default."""

from __future__ import annotations

import contextlib
import os
import pathlib
import sys

import psycopg

DEFAULT_DSN = "postgresql://lab:lab@localhost:5435/lab"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def dsn() -> str:
    return os.environ.get("LAB_DSN", DEFAULT_DSN)


@contextlib.contextmanager
def connect(*, autocommit: bool = False):
    """Open a connection, or explain how to start the database and exit.

    A stack trace from deep inside a driver is a bad first experience for
    someone whose only mistake was not running `./lab.sh up`.
    """
    try:
        conn = psycopg.connect(dsn(), autocommit=autocommit)
    except psycopg.OperationalError as exc:
        raise SystemExit(
            f"Cannot reach Postgres at {dsn()}\n"
            f"  {str(exc).strip().splitlines()[0]}\n\n"
            "Start it with:  ./lab.sh up\n"
            "Or point LAB_DSN at your own instance (it needs the `vector` extension)."
        ) from None
    try:
        yield conn
    finally:
        conn.close()


def require_data(conn) -> None:
    """Fail early and usefully when the tables exist but are empty."""
    jobs = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    cands = conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
    if jobs == 0 or cands == 0:
        raise SystemExit(
            f"Database has {jobs} jobs and {cands} candidates.\n"
            "Load the checked-in snapshot with:  ./lab.sh load"
        )


def require_embeddings(conn) -> None:
    missing_j = conn.execute(
        "SELECT count(*) FROM job_docs WHERE embedding IS NULL"
    ).fetchone()[0]
    missing_c = conn.execute(
        "SELECT count(*) FROM resumes WHERE embedding IS NULL"
    ).fetchone()[0]
    if missing_j or missing_c:
        raise SystemExit(
            f"{missing_j} jobs and {missing_c} resumes have no embedding.\n"
            "Run:  ./lab.sh embed"
        )


def table_counts(conn) -> dict[str, int]:
    tables = ("candidates", "resumes", "employers", "jobs", "job_docs", "interactions")
    return {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608 - fixed list
        for t in tables
    }


def progress(label: str, done: int, total: int, *, every: int = 200) -> None:
    """Single-line progress that stays quiet when output is redirected."""
    if not sys.stderr.isatty():
        return
    if done % every and done != total:
        return
    pct = 100 * done / total if total else 100
    print(f"\r  {label}: {done}/{total} ({pct:.0f}%)", end="", file=sys.stderr, flush=True)
    if done == total:
        print(file=sys.stderr)
