# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "psycopg[binary]", "redis"]
# ///
"""Scenario 06 — a long-lived connection is a held resource, all day."""

import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import GATEWAY, Lab, compose, reset, token_for  # noqa: E402

ALICE = token_for("alice")
POOL_SIZE = 10  # app/adapters/postgres.py: pool_size=10, max_overflow=0
STREAMS = 12  # two more than the pool holds

lab = Lab(
    "06 — THE CONNECTION BUDGET",
    "Twelve idle browser tabs took down an endpoint that has nothing to do with feeds.",
)
reset()


def probe_latency(actor, path: str, samples: int = 5) -> tuple[float, bool]:
    """How long an ORDINARY request takes while the streams are open."""
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        try:
            response = actor.client.get(
                f"{GATEWAY}{path}",
                headers={"Authorization": f"Bearer {ALICE}"},
                timeout=6.0,
            )
            timings.append((time.perf_counter() - started) * 1000)
            if response.status_code >= 500:
                return (statistics.median(timings), False)
        except Exception:
            return (6000.0, False)
    return (statistics.median(timings), True)


ordinary = lab.actor("client", "alice")


# ---------------------------------------------------------------------------
lab.section("Baseline: an ordinary request, with nothing else going on")

baseline, ok = probe_latency(ordinary, "/datasets")
lab.measure("GET /datasets, idle server:", f"{baseline:.0f}ms median")


# ---------------------------------------------------------------------------
lab.section("The anomaly: SSE handlers that hold a pooled session")
lab.note(
    f"""
    `?hold_session=true` makes the feed endpoint do what a request-scoped
    dependency does by default:

        async def feed(session: AsyncSession = Depends(get_session)):
            return StreamingResponse(...)

    FastAPI acquires the session when the request starts and releases it when
    the RESPONSE ENDS. For a normal endpoint that is milliseconds. For SSE it
    is however long the user leaves the tab open.

    The pool holds {POOL_SIZE} connections. Opening {STREAMS} streams.
    """
)

holders = [
    lab.stream(
        f"tab{i}",
        f"{GATEWAY}/feed?block_ms=5000&hold_session=true",
        token=ALICE,
        trace=False,
    ).open()
    for i in range(STREAMS)
]
time.sleep(4)  # let every stream establish and take its connection

starved, succeeded = probe_latency(ordinary, "/datasets", samples=3)
lab.measure(f"GET /datasets, with {STREAMS} streams open:", f"{starved:.0f}ms" + ("" if succeeded else "  (FAILED)"))
lab.measure("slowdown:", f"{starved / max(baseline, 0.1):.0f}x")

lab.broke(
    not succeeded or starved > baseline * 20,
    f"an unrelated endpoint went from {baseline:.0f}ms to {starved:.0f}ms",
    f"""
    {STREAMS} people opened a feed and left it open. They are not doing
    anything — no events, no queries, no CPU. And `/datasets`, which never
    touches the feed, is now starved, because every one of those idle tabs is
    holding one of the {POOL_SIZE} connections your entire service shares.

    The failure has three properties that make it hard to diagnose:

      it scales with IDLE users, not with traffic, so your requests-per-second
        graph is flat while everything gets worse;
      it appears at the SIZE OF THE POOL, which is a number nobody thinks of as
        a user limit;
      the endpoint that breaks is never the one you changed.

    And it does not stop at the pool. Each of those sessions is holding a
    transaction open, so `DROP TABLE` in your next migration queues behind a
    browser tab in another timezone, and your deploy hangs.
    """,
)

for holder in holders:
    holder.disconnect()
# Closing the client sockets is what actually returns the sessions: the server
# releases them when the request task is cancelled, not when we stop reading.
time.sleep(6)


# ---------------------------------------------------------------------------
lab.section("The fix is a type signature, not a tuning knob")
lab.note(
    """
    The streaming use case receives a FACTORY, not a repository:

        class StreamFeed:
            def __init__(self, datasets: DatasetRepositoryFactory, log: EventLog)

        async with self.datasets() as datasets:      # microseconds
            dataset = await datasets.get(...)

    It opens a session only while it is actually querying. The connection is
    held for the duration of a QUERY, not the duration of a CONNECTION — which
    is the distinction the whole scenario is about.

    Note that in the default read mode there is no query at all: fan-out
    already decided who may see the event, so the read path never touches
    Postgres. Scenario 02's design choice pays for itself again here.
    """
)

reset()
free = [
    lab.stream(f"ok{i}", f"{GATEWAY}/feed?block_ms=5000", token=ALICE, trace=False).open()
    for i in range(STREAMS)
]
time.sleep(4)

healthy, ok = probe_latency(ordinary, "/datasets", samples=5)
lab.measure(f"GET /datasets, with {STREAMS} streams open:", f"{healthy:.0f}ms median")

lab.held(
    ok and healthy < max(baseline * 5, 250),
    f"the same {STREAMS} streams cost the unrelated endpoint nothing ({healthy:.0f}ms)",
    """
    Same number of connections, same server, same pool. The difference is when
    the session is acquired.
    """,
)


# ---------------------------------------------------------------------------
lab.section("What one idle connection actually costs")

sockets = compose(
    "exec", "-T", "api", "python", "-c",
    "import os; print(len(os.listdir('/proc/1/fd')))",
)
open_fds = int(sockets.stdout.strip() or 0)

for stream in free:
    stream.disconnect()
time.sleep(3)

after = compose(
    "exec", "-T", "api", "python", "-c",
    "import os; print(len(os.listdir('/proc/1/fd')))",
)
idle_fds = int(after.stdout.strip() or 0)

lab.measure(f"file descriptors with {STREAMS} streams:", str(open_fds))
lab.measure("file descriptors after they close:", str(idle_fds))
lab.measure("per connection:", f"~{max(open_fds - idle_fds, 0) / STREAMS:.1f} fds")

lab.held(
    open_fds > idle_fds,
    f"{STREAMS} streams held {open_fds - idle_fds} file descriptors, released on disconnect",
    """
    The inventory for ONE idle SSE connection, none of which a request-per-
    second graph will show you:

      a socket and its kernel buffers        ~10-60 KB, and an fd against the
                                             process limit (default 1024 in
                                             many base images — that is your
                                             real concurrency ceiling)
      an asyncio task and its generator      small, but it is never collected
                                             while the client is connected
      a Redis connection blocked in XREAD    one per stream, against Redis's
                                             maxclients (default 10,000)
      a pooled database session              ZERO, if you got the factory right
      a slot in every proxy in the path      Traefik, the ingress, the load
                                             balancer — each has its own limit

    So capacity planning for a push feed is not "requests per second". It is
    "concurrent connections", and it is bounded by the SMALLEST of those five
    numbers — which is usually one you have never looked at.
    """,
)

lab.note(
    """
    What to do about it, in the order you should reach for them:

      1. Hold nothing per connection that you can acquire per operation.
         Sessions, transactions, file handles, locks. This is free and it is
         the whole fix for most services.

      2. Cap concurrency explicitly, per subject. A rate limiter counts
         requests and one SSE request is a thousand events, so it does not help
         here: you need "at most N streams per user", which also stops one
         person with fifty tabs from being your capacity problem.

      3. Size the pieces against connections, not requests. File descriptor
         limits, Redis maxclients, proxy connection limits, worker counts.

      4. Then, and only then, consider whether you needed a push channel at
         all. Ten thousand idle SSE connections is real infrastructure. A
         thirty-second poll against a cached endpoint is a cron job, costs
         nothing to operate, and for plenty of "realtime" features is
         indistinguishable to the user. Choosing SSE should be a decision, not
         a default — and the honest version of this lab says so.
    """
)

lab.takeaway(
    f"""
    "A long-lived connection is a held resource for as long as it is open, so
     the question stops being requests per second and becomes what each
     connection holds. The one that bites is a request-scoped database session:
     FastAPI releases it when the response ends, and an SSE response ends when
     the user closes the tab — so {STREAMS} idle browser tabs exhausted a pool
     of {POOL_SIZE} and starved an endpoint that has nothing to do with the
     feed. It scales with idle users rather than traffic, so nothing on the
     traffic graph explains it, and the open transactions also block the next
     migration's DDL.

     The fix is a type signature: the streaming use case takes a repository
     FACTORY and opens a session only while it queries. After that, capacity is
     bounded by whichever is smallest — file descriptors, Redis maxclients, or
     a proxy's connection limit — and I cap concurrent streams per subject,
     because a rate limiter counts requests and one stream is one request."
    """
)

lab.finish()
