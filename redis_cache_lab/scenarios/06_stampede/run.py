# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 06 — the thundering herd one microsecond after an invalidation."""

import pathlib
import random
import sys
import threading
import time
import uuid

import psycopg
import redis as redis_lib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.cache import lock_key, user_key  # noqa: E402
from lab.harness import DSN, REDIS_URL, Lab, redis_client, reset  # noqa: E402

USER = 1
HERD = 40  # concurrent requests for the same key
QUERY_MS = 150  # how slow the thing you are caching actually is

lab = Lab(
    "06 — THE STAMPEDE",
    "Correctness is fine. It is the moment AFTER the invalidation that hurts.",
)
reset()

rdb = redis_client()
db_reads = 0
db_lock = threading.Lock()


def slow_db_read() -> str:
    """The query the cache exists to avoid. Counted, so we can see the herd."""
    global db_reads
    with db_lock:
        db_reads += 1
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SELECT pg_sleep(%s)", (QUERY_MS / 1000.0,))
        return conn.execute("SELECT role FROM users WHERE id = %s", (USER,)).fetchone()[0]


def run_herd(worker) -> float:
    """Fire HERD requests at the same key at the same instant, return wall seconds."""
    global db_reads
    db_reads = 0
    barrier = threading.Barrier(HERD)
    results: list = [None] * HERD

    def one(i):
        barrier.wait()  # everybody arrives together, like a real traffic spike
        results[i] = worker()

    threads = [threading.Thread(target=one, args=(i,)) for i in range(HERD)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    assert all(r == "guest" for r in results), f"herd returned junk: {set(results)}"
    return elapsed


# ---------------------------------------------------------------------------
lab.section("The anomaly: everyone misses at once")
lab.note(
    f"""
    The key was just invalidated — by a legitimate write, or by the TTL, or by
    an eviction. {HERD} in-flight requests want it. Every one of them misses,
    and every one of them goes to Postgres.

    Nothing here is incorrect. Every request returns the right answer. The
    database simply receives {HERD}x the load it was sized for, in a burst, at
    the worst possible moment — right after a write.
    """
)


def naive_worker():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        cached = client.get(user_key(USER))
        if cached is not None:
            return cached
        value = slow_db_read()
        client.set(user_key(USER), value, ex=300)
        return value
    finally:
        client.close()


rdb.delete(user_key(USER))
naive_elapsed = run_herd(naive_worker)
naive_reads = db_reads

lab.measure("requests:", str(HERD))
lab.measure("database reads:", str(naive_reads))
lab.measure("wall clock:", f"{naive_elapsed * 1000:.0f}ms")

lab.broke(
    naive_reads > HERD * 0.8,
    f"{naive_reads} of {HERD} requests hit the database for the same key",
    f"""
    The nastiest property of this failure is that it is SELF-REINFORCING. The
    {naive_reads} concurrent queries make the database slower, which widens the
    window in which new arrivals also miss, which adds more queries. A cache
    hit rate of 99% does not protect you: this is entirely about what happens
    during the 1%.

    It is also why "we added a cache and now the database falls over during
    deploys" is a real sentence. A deploy empties an in-process cache, or
    restarts every pod at once, and every key becomes cold simultaneously.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Fix: single-flight — one request per key pays for the miss")
lab.note(
    """
    `SET lock:<key> <token> NX PX 2000` is the whole mechanism: an atomic
    test-and-set with a self-healing expiry. The winner reads the database and
    fills the cache. The losers wait for it, then read the cache.

    Two details that are not optional:

      PX (a TTL on the lock)     without it, a holder that crashes wedges the
                                 key forever, and forever is a long time.
      compare-and-delete on release  proven below — a plain DEL frees someone
                                 ELSE's lock if your work overran the TTL.
    """
)


def single_flight_worker():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    key, lkey = user_key(USER), lock_key(USER)
    try:
        cached = client.get(key)
        if cached is not None:
            return cached
        token = uuid.uuid4().hex
        if client.set(lkey, token, nx=True, px=3000):
            try:
                value = slow_db_read()
                client.set(key, value, ex=300)
                return value
            finally:
                # Compare-and-delete, never a plain DEL. See the next section.
                client.eval(
                    "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                    1, lkey, token,
                )
        # Loser: wait for the winner's answer rather than duplicating its work.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cached = client.get(key)
            if cached is not None:
                return cached
            time.sleep(0.005)
        return slow_db_read()  # winner died; a cache must never become an outage
    finally:
        client.close()


rdb.delete(user_key(USER))
rdb.delete(lock_key(USER))
sf_elapsed = run_herd(single_flight_worker)
sf_reads = db_reads

lab.measure("requests:", str(HERD))
lab.measure("database reads:", str(sf_reads))
lab.measure("wall clock:", f"{sf_elapsed * 1000:.0f}ms")
lab.measure("database load reduction:", f"{naive_reads / max(sf_reads, 1):.0f}x")

lab.held(
    sf_reads == 1,
    f"exactly {sf_reads} database read served all {HERD} requests",
    f"""
    Wall clock barely moved ({naive_elapsed * 1000:.0f}ms → {sf_elapsed * 1000:.0f}ms)
    because the query time dominates either way. That is the point worth
    noticing: single-flight is not a latency optimisation. It is a LOAD
    optimisation, and load is what takes the database down.

    The tradeoff you are choosing: {HERD - 1} requests now wait on someone
    else's query instead of running their own. For a user-blocking read that
    is right. For a dashboard, serving the stale value while one request
    refreshes in the background is better. Blocking versus stale-while-
    revalidate is a product decision, not a Redis one.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Why the lock release must be compare-and-delete")
lab.note(
    """
    Holder A takes the lock with a 100ms expiry, then its query takes 150ms —
    a GC pause, a slow plan, a network stall. The lock expires. B takes it. A
    finishes and releases with a plain DEL.

    A has just unlocked B's lock, and there is now no lock at all while B is
    still working. Two leaders, and the failure looks exactly like the bug you
    added the lock to fix.
    """
)

lkey = lock_key(999)
rdb.delete(lkey)
a_token, b_token = "token-A", "token-B"

rdb.set(lkey, a_token, nx=True, px=100)
time.sleep(0.15)  # A's work overruns; the lock expires
got_b = rdb.set(lkey, b_token, nx=True, px=3000)
rdb.delete(lkey)  # A releases with a plain DEL — the bug
holder_after_naive_del = rdb.get(lkey)

lab.broke(
    got_b is True and holder_after_naive_del is None,
    "a plain DEL released a lock that had already been handed to someone else",
    """
    Note what this costs you: the lock is not merely useless, it is
    ACTIVELY misleading. Everyone believes there is mutual exclusion, and the
    incident review will start by ruling out the lock because "we take a lock
    there".
    """,
)

rdb.delete(lkey)
rdb.set(lkey, a_token, nx=True, px=100)
time.sleep(0.15)
rdb.set(lkey, b_token, nx=True, px=3000)
rdb.eval(
    "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
    1, lkey, a_token,  # A tries to release, atomically checking it still holds
)
holder_after_cad = rdb.get(lkey)

lab.held(
    holder_after_cad == b_token,
    "compare-and-delete: A's release was a no-op, B still holds the lock",
    """
    It has to be a script, not GET-then-DEL from the client, for the same
    reason as everything else in this lab: two round trips have a gap, and the
    gap is where the bug lives. Redis runs a script with nothing interleaved.

    (And the honest caveat: this is single-instance mutual exclusion. Across a
    failover it is not safe, because the new primary may not have the lock.
    Redlock exists and is contested. If correctness — not just load — depends
    on mutual exclusion, use a Postgres advisory lock or a unique constraint,
    which is what the avatar_uploading lab in this repo is about.)
    """,
)


# ---------------------------------------------------------------------------
lab.section("The synchronised expiry you built yourself")
lab.note(
    """
    A deploy warms 10,000 keys in one second, all with `ex=300`. Five minutes
    later they all expire in the same second — and you get this same stampede
    across every key at once, on a five-minute cycle, forever.

    One line fixes it. Measured below on 400 keys with a 1-second TTL.
    """
)

reset()
for i in range(400):
    rdb.set(f"fixed:{i}", "x", px=1000)
    rdb.set(f"jittered:{i}", "x", px=random.randint(1000, 3000))

time.sleep(1.3)
fixed_left = sum(1 for _ in rdb.scan_iter("fixed:*"))
jittered_left = sum(1 for _ in rdb.scan_iter("jittered:*"))

lab.measure("fixed ttl=1000ms,   alive after 1.3s:", f"{fixed_left} of 400")
lab.measure("jittered 1000-3000ms, alive after 1.3s:", f"{jittered_left} of 400")

lab.held(
    fixed_left == 0 and jittered_left > 100,
    "jitter spread the expiries; the fixed TTL expired them in one burst",
    """
        ttl = base + random.randint(0, base // 5)

    That is the whole fix. Do it everywhere you set a TTL, and especially in
    the code that warms the cache at startup — that is the one that creates
    ten thousand keys inside the same second.
    """,
)

lab.note(
    """
    The other two tools, so you can name them:

      stale-while-revalidate   Serve the expired value and refresh in the
                               background. Zero user-visible latency, at the
                               cost of a deliberately stale response. Needs a
                               second TTL ("hard expiry") so stale cannot be
                               served forever.

      probabilistic early
      expiry (XFetch)          Each reader recomputes with probability rising
                               as the TTL approaches, so ONE reader refreshes
                               early and the rest keep hitting. No lock, no
                               coordination. Elegant, and harder to explain in
                               an incident review at 3am — which is a real
                               engineering cost, not a joke.
    """
)

lab.takeaway(
    f"""
    "A cache protects the database at steady state and stops protecting it at
     exactly the moment a key is invalidated — every in-flight request misses
     at once and they all query the same row. In this lab {HERD} concurrent
     requests became {naive_reads} database reads; with single-flight — SET NX
     PX, winner fills the cache, losers wait — it became {sf_reads}. That is a
     load fix, not a latency fix: wall clock barely changed. The lock needs a
     TTL so a crashed holder can't wedge the key, and the release has to be a
     compare-and-delete in Lua, or an overrunning holder frees somebody else's
     lock. And I jitter every TTL, because ten thousand keys warmed in the same
     second expire in the same second."
    """
)

rdb.close()
lab.finish()
