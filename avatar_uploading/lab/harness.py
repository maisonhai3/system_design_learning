"""Shared plumbing for the scenario runners.

Every scenario's `run.py` uses this to do the same three things:

  1. Reset the database so the run is reproducible.
  2. Drive two or more sessions through a *specific* interleaving, printing
     each statement as it happens so the output reads like a trace.
  3. Assert that the anomaly actually reproduced and that the fix actually
     held — and exit non-zero if either claim turns out to be false.

Point 3 is the important one. A teaching lab that quietly stops demonstrating
its own bug (because a Postgres version changed, or a fix was too eager) is
worse than no lab, because you would go into the interview still believing it.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

import psycopg

LAB_ROOT = pathlib.Path(__file__).resolve().parents[1]
DSN = os.environ.get("LAB_DSN", "postgresql://lab:lab@localhost:5433/lab")

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


BOLD = lambda s: _c("1", s)
DIM = lambda s: _c("2", s)
RED = lambda s: _c("31", s)
GREEN = lambda s: _c("32", s)
YELLOW = lambda s: _c("33", s)

_SESSION_COLORS = ["36", "35", "33", "34", "32"]  # cyan, magenta, yellow, blue, green
_assigned: dict[str, str] = {}


def _session_color(name: str) -> str:
    if name not in _assigned:
        _assigned[name] = _SESSION_COLORS[len(_assigned) % len(_SESSION_COLORS)]
    return _assigned[name]


def _fmt_rows(rows) -> str:
    """Render a result set compactly enough to sit at the end of a trace line."""
    if rows is None:
        return ""
    if not rows:
        return "(0 rows)"
    if len(rows) == 1 and len(rows[0]) == 1:
        return repr(rows[0][0])
    if len(rows) <= 4:
        return "; ".join(", ".join(repr(v) for v in r) for r in rows)
    return f"({len(rows)} rows)"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session:
    """One database connection, driven with explicit SQL.

    Deliberately runs in autocommit mode and issues literal ``BEGIN`` /
    ``COMMIT`` statements instead of using the driver's transaction context
    manager. That way the printed trace is exactly what you would type into
    the two psql terminals in `a.sql` and `b.sql` — the automated proof and
    the manual exercise stay honestly in sync.
    """

    def __init__(self, name: str, dsn: str = DSN):
        self.name = name
        self.conn = psycopg.connect(dsn, autocommit=True, application_name=f"session_{name}")
        self.pid = self.conn.info.backend_pid
        # Display only. Server-side binding still does the real execution; this
        # just renders parameters inline so the trace reads like typed SQL
        # rather than like a prepared statement full of %s.
        self._mogrifier = psycopg.ClientCursor(self.conn)

    def _display(self, sql: str, params=None) -> str:
        if params:
            try:
                sql = self._mogrifier.mogrify(sql, params)
            except Exception:
                pass
        return " ".join(sql.split())

    # -- plumbing -----------------------------------------------------------

    def _tag(self) -> str:
        return _c(_session_color(self.name), f"{self.name:>3}")

    def log(self, text: str, trailer: str = "") -> None:
        line = f"  {self._tag()} {DIM('│')} {text}"
        if trailer:
            line += f"  {trailer}"
        print(line, flush=True)

    def run(self, sql: str, params=None, note: str | None = None):
        """Execute one statement, print it, return its rows (or None)."""
        flat = self._display(sql, params)
        try:
            cur = self.conn.execute(sql, params)
        except psycopg.Error as exc:
            code = getattr(exc, "sqlstate", None) or "?????"
            self.log(flat, RED(f"✗ {code} {type(exc).__name__}"))
            raise
        rows = cur.fetchall() if cur.description else None
        trailer = ""
        if rows is not None:
            trailer = DIM("→ ") + _fmt_rows(rows)
        elif cur.rowcount and cur.rowcount > 0:
            trailer = DIM(f"→ {cur.rowcount} row(s) affected")
        if note:
            trailer += DIM(f"   {note}")
        self.log(flat, trailer)
        return rows

    def scalar(self, sql: str, params=None, note: str | None = None):
        rows = self.run(sql, params, note=note)
        return rows[0][0] if rows else None

    def try_run(self, sql: str, params=None) -> psycopg.Error | None:
        """Run a statement that is *expected* to fail. Returns the error."""
        try:
            self.run(sql, params)
            return None
        except psycopg.Error as exc:
            return exc

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # -- concurrency --------------------------------------------------------

    def spawn(self, sql: str, params=None) -> "Pending":
        """Issue a statement on its own thread, so you can watch it block."""
        return Pending(self, sql, params)


class Pending:
    """A statement running on a background thread.

    Used for the half of these scenarios where the interesting behaviour is
    *not returning* — a transaction parked on a row lock, a deadlock victim
    waiting for the deadlock detector to fire.
    """

    def __init__(self, session: Session, sql: str, params=None):
        self.session = session
        self.sql = session._display(sql, params)
        self.rows = None
        self.error: psycopg.Error | None = None
        self._done = threading.Event()
        session.log(self.sql, DIM("… issued, waiting"))
        self._thread = threading.Thread(target=self._work, args=(sql, params), daemon=True)
        self._thread.start()

    def _work(self, sql, params):
        try:
            cur = self.session.conn.execute(sql, params)
            self.rows = cur.fetchall() if cur.description else None
        except psycopg.Error as exc:
            self.error = exc
        finally:
            self._done.set()

    def finished(self) -> bool:
        return self._done.is_set()

    def still_blocked_after(self, seconds: float) -> bool:
        """True if the statement is *still* waiting after `seconds`."""
        blocked = not self._done.wait(timeout=seconds)
        if blocked:
            self.session.log(self.sql, YELLOW(f"⧗ still blocked after {seconds:g}s"))
        return blocked

    def wait(self, timeout: float = 15.0) -> "Pending":
        if not self._done.wait(timeout=timeout):
            raise TimeoutError(f"[{self.session.name}] never returned: {self.sql}")
        if self.error is not None:
            code = getattr(self.error, "sqlstate", None) or "?????"
            self.session.log(self.sql, RED(f"✗ {code} {type(self.error).__name__}"))
        else:
            self.session.log(self.sql, GREEN("✓ unblocked ") + DIM(_fmt_rows(self.rows)))
        return self


# ---------------------------------------------------------------------------
# Database control
# ---------------------------------------------------------------------------


def reset() -> None:
    """Re-apply schema + seed. Every runner calls this first."""
    with psycopg.connect(DSN, autocommit=True, application_name="lab_reset") as conn:
        for name in ("01_schema.sql", "02_seed.sql"):
            conn.execute((LAB_ROOT / "schema" / name).read_text())


def blocking_report() -> list[tuple]:
    """Who is waiting on whom, right now."""
    with psycopg.connect(DSN, autocommit=True, application_name="lab_observer") as conn:
        return conn.execute(
            """
            SELECT a.application_name,
                   a.wait_event_type || ':' || a.wait_event AS waiting_on,
                   pg_blocking_pids(a.pid)                  AS blocked_by,
                   left(a.query, 60)                        AS query
            FROM pg_stat_activity a
            WHERE cardinality(pg_blocking_pids(a.pid)) > 0
            """
        ).fetchall()


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class Lab:
    """Collects claims and turns them into an exit code.

    Two kinds of claim, and both must hold for the run to pass:

      broke(...)  the naive version MUST exhibit the bug. If it silently
                  behaves itself, this lab is no longer teaching anything.
      held(...)   the fix MUST work. Obviously.
    """

    def __init__(self, title: str, subtitle: str = ""):
        self.title = title
        self.failures: list[str] = []
        self.passes = 0
        self._sessions: list[Session] = []
        print()
        print(BOLD(f"┏━ {title}"))
        if subtitle:
            print(DIM(f"┃  {subtitle}"))
        print()

    def session(self, name: str, dsn: str = DSN) -> Session:
        s = Session(name, dsn)
        self._sessions.append(s)
        return s

    def section(self, title: str) -> None:
        print()
        print(BOLD(f"── {title} " + "─" * max(0, 62 - len(title))))

    def note(self, text: str) -> None:
        for line in text.strip().splitlines():
            print(DIM(f"     {line.strip()}"))

    def _record(self, ok: bool, kind: str, description: str, detail: str) -> None:
        if ok:
            self.passes += 1
            print(f"  {GREEN('✓')} {kind}: {description}")
        else:
            self.failures.append(f"{kind}: {description}")
            print(f"  {RED('✗')} {kind}: {description}")
        if detail:
            print(DIM(f"      {detail}"))

    def broke(self, condition: bool, description: str, detail: str = "") -> bool:
        """Assert the anomaly reproduced."""
        self._record(condition, "ANOMALY REPRODUCED", description, detail)
        return condition

    def held(self, condition: bool, description: str, detail: str = "") -> bool:
        """Assert the fix worked."""
        self._record(condition, "FIX HELD", description, detail)
        return condition

    def takeaway(self, text: str) -> None:
        print()
        print(BOLD("  Say this out loud:"))
        for line in text.strip().splitlines():
            print(f"    {line.strip()}")

    def finish(self) -> None:
        for s in self._sessions:
            s.close()
        print()
        if self.failures:
            print(RED(BOLD(f"┗━ {self.title}: {len(self.failures)} claim(s) did not hold")))
            for f in self.failures:
                print(RED(f"     - {f}"))
            print(DIM("   The lab is no longer demonstrating what it says it demonstrates."))
            sys.exit(1)
        print(GREEN(BOLD(f"┗━ {self.title}: {self.passes}/{self.passes} claims held")))
        sys.exit(0)


def bootstrap() -> None:
    """Put the lab package on sys.path. Called by each run.py before importing."""
    sys.path.insert(0, str(LAB_ROOT))
