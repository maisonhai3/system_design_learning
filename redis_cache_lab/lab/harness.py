"""Shared plumbing for the scenario runners.

Every scenario's `run.py` uses this to do the same three things:

  1. Reset both stores, so the run is reproducible: re-apply schema + seed to
     Postgres, FLUSHDB Redis.
  2. Drive two or more actors through a *specific* interleaving, printing each
     Postgres statement and each Redis command as it happens, so the output
     reads like a trace of one incident.
  3. Assert that the anomaly actually reproduced and that the fix actually
     held — and exit non-zero if either claim turns out to be false.

Point 3 is the one that matters. A teaching lab that quietly stops
demonstrating its own bug — because a library changed, or a fix was too eager —
is worse than no lab, because you would walk into the interview still
believing it. `./lab.sh run-all` exits non-zero the moment that happens.

A note on determinism, since the JD asks for tests that prove a race:
`time.sleep()` does not prove anything. It makes a race *likely*, which is the
same as making a test flaky, and a flaky test is a test that gets deleted. The
scenarios here force the interleaving instead, with `Latch` — a named pause
point the actor blocks on until another thread lets it through. Same idea as
`pg_sleep` in a SQL lab or a mocked clock in a timeout test: to prove ordering
you must control ordering.
"""

from __future__ import annotations

import os
import pathlib
import sys
import textwrap
import threading
import time

import psycopg
import redis

LAB_ROOT = pathlib.Path(__file__).resolve().parents[1]
DSN = os.environ.get("LAB_DSN", "postgresql://lab:lab@localhost:5436/lab")
REDIS_URL = os.environ.get("LAB_REDIS_URL", "redis://localhost:6380/0")

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
CYAN = lambda s: _c("36", s)

_SESSION_COLORS = ["36", "35", "33", "34", "32"]  # cyan, magenta, yellow, blue, green
_assigned: dict[str, str] = {}
_print_lock = threading.Lock()


def _session_color(name: str) -> str:
    if name not in _assigned:
        _assigned[name] = _SESSION_COLORS[len(_assigned) % len(_SESSION_COLORS)]
    return _assigned[name]


def _fmt(value) -> str:
    """Render a result compactly enough to sit at the end of a trace line."""
    if value is None:
        return "None"
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str) and len(value) > 58:
        return repr(value[:55] + "...")
    if isinstance(value, list):
        if not value:
            return "(empty)"
        if len(value) == 1 and isinstance(value[0], tuple) and len(value[0]) == 1:
            return repr(value[0][0])
        if len(value) <= 4:
            return "; ".join(
                ", ".join(repr(v) for v in r) if isinstance(r, tuple) else repr(r)
                for r in value
            )
        return f"({len(value)} items)"
    return repr(value)


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


class Session:
    """One actor: a Postgres connection plus a Redis connection.

    Both are held by the same object on purpose. Real cache bugs are not
    "a Postgres bug" or "a Redis bug" — they are an *ordering* bug between two
    stores that know nothing about each other. Attributing every line to the
    actor who issued it is what makes the ordering visible.

    Deliberately runs Postgres in autocommit mode and issues literal ``BEGIN``
    and ``COMMIT`` statements rather than using the driver's transaction
    context manager, so the printed trace is exactly the sequence you would
    type by hand.
    """

    def __init__(self, name: str, dsn: str = DSN, redis_url: str = REDIS_URL):
        self.name = name
        self.db = psycopg.connect(dsn, autocommit=True, application_name=f"lab_{name}")
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.pid = self.db.info.backend_pid
        # Display only. Server-side binding still does the real execution; this
        # renders parameters inline so the trace reads like typed SQL.
        self._mogrifier = psycopg.ClientCursor(self.db)
        self.db_reads = 0  # how many times this actor hit the source of truth

    # -- trace --------------------------------------------------------------

    def _tag(self) -> str:
        return _c(_session_color(self.name), f"{self.name:>4}")

    def log(self, text: str, trailer: str = "") -> None:
        line = f"  {self._tag()} {DIM('│')} {text}"
        if trailer:
            line += f"  {trailer}"
        with _print_lock:
            print(line, flush=True)

    def say(self, text: str) -> None:
        """A narration line from this actor, not a command."""
        self.log(DIM(f"# {text}"))

    # -- postgres -----------------------------------------------------------

    def _display(self, sql: str, params=None) -> str:
        if params:
            try:
                sql = self._mogrifier.mogrify(sql, params)
            except Exception:
                pass
        return " ".join(sql.split())

    def sql(self, statement: str, params=None, note: str | None = None):
        """Execute one statement, print it, return its rows (or None)."""
        flat = self._display(statement, params)
        try:
            cur = self.db.execute(statement, params)
        except psycopg.Error as exc:
            code = getattr(exc, "sqlstate", None) or "?????"
            self.log(flat, RED(f"✗ {code} {type(exc).__name__}"))
            raise
        rows = cur.fetchall() if cur.description else None
        trailer = ""
        if rows is not None:
            self.db_reads += 1
            trailer = DIM("→ ") + _fmt(rows)
        elif cur.rowcount and cur.rowcount > 0:
            trailer = DIM(f"→ {cur.rowcount} row(s) affected")
        if note:
            trailer += DIM(f"   {note}")
        self.log(flat, trailer)
        return rows

    def scalar(self, statement: str, params=None, note: str | None = None):
        rows = self.sql(statement, params, note=note)
        return rows[0][0] if rows else None

    # -- redis --------------------------------------------------------------
    #
    # Thin traced wrappers rather than a full facade. You should be able to
    # read a scenario and know exactly which Redis command ran, because in an
    # incident that is the only thing you will have to reason about.

    def _redis(self, label: str, fn, *args, note: str | None = None):
        started = time.perf_counter()
        result = fn(*args)
        ms = (time.perf_counter() - started) * 1000
        trailer = DIM("→ ") + _fmt(result)
        if ms >= 10:
            trailer += YELLOW(f"   [{ms:.0f}ms]")
        if note:
            trailer += DIM(f"   {note}")
        self.log(CYAN(label), trailer)
        return result

    def cache_get(self, key: str, note: str | None = None):
        return self._redis(f"GET {key}", self.redis.get, key, note=note)

    def cache_set(self, key: str, value: str, ttl: int | None = None, note: str | None = None):
        if ttl is None:
            # No TTL is a decision, not a default. Scenario 02 is about the day
            # that decision costs you.
            return self._redis(f"SET {key} {value!r}", self.redis.set, key, value, note=note)
        return self._redis(
            f"SETEX {key} {ttl} {value!r}",
            lambda k, t, v: self.redis.setex(k, t, v),
            key,
            ttl,
            value,
            note=note,
        )

    def cache_del(self, key: str, note: str | None = None):
        return self._redis(f"DEL {key}", self.redis.delete, key, note=note)

    def cache_ttl(self, key: str, note: str | None = None):
        return self._redis(f"TTL {key}", self.redis.ttl, key, note=note)

    def close(self) -> None:
        for closer in (self.db.close, self.redis.close):
            try:
                closer()
            except Exception:
                pass

    # -- concurrency --------------------------------------------------------

    def spawn(self, fn, *args, **kwargs) -> "Pending":
        """Run a callable on its own thread, so you can watch it block."""
        return Pending(self, fn, args, kwargs)


class Pending:
    """Work running on a background thread.

    Used for the half of these scenarios where the interesting behaviour is
    *not returning yet* — a reader parked between "read the database" and
    "write the cache", which is exactly the window a writer slips through.
    """

    def __init__(self, session: Session, fn, args=(), kwargs=None):
        self.session = session
        self.label = getattr(fn, "__name__", repr(fn))
        self.result = None
        self.error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._work, args=(fn, args, kwargs or {}), daemon=True
        )
        self._thread.start()

    def _work(self, fn, args, kwargs):
        try:
            self.result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced via .wait()
            self.error = exc
        finally:
            self._done.set()

    def finished(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float = 20.0):
        if not self._done.wait(timeout=timeout):
            raise TimeoutError(f"[{self.session.name}] {self.label} never returned")
        if self.error is not None:
            raise self.error
        return self.result


class Latch:
    """A named pause point, so an interleaving can be forced instead of hoped for.

    A cache-aside read has a window between "I read the database" and "I wrote
    the cache". The window is normally microseconds, so a test that waits for
    it by sleeping is a test that fails one build in fifty and gets deleted.

    Instead the code under test calls `latch.arrive("loaded")`, which blocks
    until the main thread calls `latch.let_through("loaded")`. The race becomes
    a sequence, the sequence is deterministic, and the proof is a proof.

    This is a *seam*, and the honest tradeoff is that it lives in the code under
    test. In the lab that is fine — `lab/cache.py` exists to be instrumented.
    In a real service you either accept a config-gated delay on the chaos path
    (what `app/` does), or you assert the ordering some other way: a Postgres
    advisory lock held by the test, an injected clock, or a fake Redis whose
    SET blocks on demand.
    """

    def __init__(self):
        self._gates: dict[str, threading.Event] = {}
        self._arrived: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _events(self, name: str) -> tuple[threading.Event, threading.Event]:
        with self._lock:
            if name not in self._gates:
                self._gates[name] = threading.Event()
                self._arrived[name] = threading.Event()
            return self._arrived[name], self._gates[name]

    def arrive(self, name: str, timeout: float = 20.0) -> None:
        """Called by the code under test. Blocks until let through."""
        arrived, gate = self._events(name)
        arrived.set()
        if not gate.wait(timeout=timeout):
            raise TimeoutError(f"latch {name!r} was never released")

    def wait_for(self, name: str, timeout: float = 20.0) -> None:
        """Block until someone reaches the pause point."""
        arrived, _ = self._events(name)
        if not arrived.wait(timeout=timeout):
            raise TimeoutError(f"nobody ever arrived at latch {name!r}")

    def let_through(self, name: str) -> None:
        _, gate = self._events(name)
        gate.set()

    def open_all(self) -> None:
        with self._lock:
            for gate in self._gates.values():
                gate.set()


# ---------------------------------------------------------------------------
# Store control
# ---------------------------------------------------------------------------


def reset() -> None:
    """Re-apply schema + seed, and empty Redis. Every runner calls this first."""
    with psycopg.connect(DSN, autocommit=True, application_name="lab_reset") as conn:
        for name in ("01_schema.sql", "02_seed.sql"):
            conn.execute((LAB_ROOT / "schema" / name).read_text())
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    client.close()


def redis_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def keyspace() -> list[str]:
    """Every key currently in Redis, sorted. Small lab, so SCAN is overkill —
    but see scenario 07 for why you would never write KEYS * in a service."""
    client = redis_client()
    try:
        return sorted(client.scan_iter("*"))
    finally:
        client.close()


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

    def session(self, name: str) -> Session:
        s = Session(name)
        self._sessions.append(s)
        return s

    def section(self, title: str) -> None:
        print()
        print(BOLD(f"── {title} " + "─" * max(0, 62 - len(title))))

    def note(self, text: str) -> None:
        # dedent, not strip-per-line: sub-lists and tables in a note keep their
        # relative indentation, which is the only thing that makes them legible.
        for line in textwrap.dedent(text).strip("\n").splitlines():
            print(DIM(f"     {line}"))

    def measure(self, label: str, value: str) -> None:
        print(f"     {BOLD(label)}  {value}")

    def _record(self, ok: bool, kind: str, description: str, detail: str) -> None:
        if ok:
            self.passes += 1
            print(f"  {GREEN('✓')} {kind}: {description}")
        else:
            self.failures.append(f"{kind}: {description}")
            print(f"  {RED('✗')} {kind}: {description}")
        if detail:
            for line in textwrap.dedent(detail).strip("\n").splitlines():
                print(DIM(f"      {line}"))

    def broke(self, condition: bool, description: str, detail: str = "") -> bool:
        """Assert the anomaly reproduced."""
        self._record(bool(condition), "ANOMALY REPRODUCED", description, detail)
        return bool(condition)

    def held(self, condition: bool, description: str, detail: str = "") -> bool:
        """Assert the fix worked."""
        self._record(bool(condition), "FIX HELD", description, detail)
        return bool(condition)

    def takeaway(self, text: str) -> None:
        print()
        print(BOLD("  Say this out loud:"))
        for line in textwrap.dedent(text).strip("\n").splitlines():
            print(f"    {line}")

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
