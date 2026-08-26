# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary,pool]"]
# ///
"""100 concurrent requests against a 50-connection server, two ways.

Naive: every request opens its own connection. Past max_connections the server
starts refusing them with 53300 too_many_connections — and note WHICH requests
fail: not the slow ones, just whoever arrived after the limit. A burst of cheap
requests can lock out an expensive one that was already halfway through.

Pooled: the same 100 requests share 20 connections. Nothing is refused; the
excess requests queue in the client for a moment. Throughput is bounded by the
pool, which is the point — a bounded queue in your app is survivable, a refused
connection at the database is not.
"""

import os
import sys
import threading
import time

import psycopg
from psycopg_pool import ConnectionPool

DSN = os.environ.get("LAB_DSN", "postgresql://lab:lab@localhost:5433/lab")
REQUESTS = 100
POOL_SIZE = 20
WORK = "SELECT pg_sleep(0.05), count(*) FROM uploads WHERE user_id = 1"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = RED = DIM = RESET = ""


def classify(exc: Exception) -> str:
    """Name the failure the way you would see it in a log.

    A connection REFUSED for lack of slots never gets an sqlstate: the server
    rejects it during startup, so psycopg raises a plain OperationalError with
    the reason only in the message text. That is worth knowing — grepping your
    logs for the SQLSTATE will not find these.
    """
    if getattr(exc, "sqlstate", None):
        return str(exc.sqlstate)
    text = str(exc).lower()
    if "too many clients" in text:
        return "too_many_clients (refused at connect)"
    if "timeout" in text or "timed out" in text:
        return "connect timeout"
    return type(exc).__name__


def run(label: str, worker, setup=None, teardown=None) -> tuple[int, dict, float]:
    ok, errors = 0, {}
    lock = threading.Lock()

    def task(i: int):
        nonlocal ok
        try:
            worker(i)
            with lock:
                ok += 1
        except Exception as exc:  # noqa: BLE001
            with lock:
                code = classify(exc)
                errors[code] = errors.get(code, 0) + 1

    if setup:
        setup()
    threads = [threading.Thread(target=task, args=(i,)) for i in range(REQUESTS)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    if teardown:
        teardown()

    status = f"{GREEN}{ok}{RESET}/{REQUESTS} succeeded"
    if errors:
        detail = ", ".join(f"{RED}{n}x {c}{RESET}" for c, n in sorted(errors.items()))
        status += f"   {detail}"
    print(f"  {label:<44} {status}   {DIM}{elapsed:.2f}s{RESET}")
    return ok, errors, elapsed


limit = int(
    psycopg.connect(DSN).execute("SHOW max_connections").fetchone()[0]
)
print(f"\n  server max_connections = {limit}, offering it {REQUESTS} concurrent requests\n")


# --- naive: one connection per request -------------------------------------
def naive(_i):
    with psycopg.connect(DSN, connect_timeout=10) as conn:
        conn.execute(WORK).fetchall()


naive_ok, naive_errors, _ = run("one connection per request", naive)


# --- pooled: 100 requests share POOL_SIZE connections -----------------------
pool = ConnectionPool(DSN, min_size=5, max_size=POOL_SIZE, open=False)


def pooled(_i):
    with pool.connection() as conn:
        conn.execute(WORK).fetchall()


pooled_ok, pooled_errors, pooled_secs = run(
    f"shared pool of {POOL_SIZE} connections",
    pooled,
    setup=lambda: (pool.open(), pool.wait(timeout=15)),
    teardown=pool.close,
)

print()
refused = REQUESTS - naive_ok
if refused:
    print(f"  The naive run had {refused} of {REQUESTS} connections REFUSED outright.")
    print("  Not the slow requests — whoever happened to arrive after the limit was")
    print("  already reached. A burst of cheap requests can lock out an expensive one")
    print("  that was already halfway through its work.")
    print()
    print("  Note the error name above: a refusal at connect time carries NO sqlstate,")
    print("  because the server rejects it during startup. Grepping your logs for a")
    print("  SQLSTATE will not find these.")
else:
    print(f"  The naive run did not trip the limit here — all {naive_ok} got through.")
    print(f"  Raise REQUESTS above {limit}, or lower max_connections in docker-compose.yml.")

print(f"  The pooled run completed {pooled_ok}/{REQUESTS} with nothing refused, in {pooled_secs:.2f}s,")
print(f"  because the excess queued in the client instead of at the server.")
print()
print("  The point to make in an interview: a pool does not make the database")
print("  faster. It converts an unbounded, server-side failure into a bounded,")
print("  client-side wait — which is the difference between a slow API and a")
print("  down one. And it is why `max_connections = 5000` is not the fix:")
print("  every backend costs memory and adds to the work of every snapshot.")

sys.exit(0 if pooled_ok == REQUESTS else 1)
