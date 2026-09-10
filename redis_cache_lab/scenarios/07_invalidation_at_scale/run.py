# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 07 — invalidating a family of keys, and the two ways it goes wrong."""

import pathlib
import statistics
import sys
import threading
import time

import redis as redis_lib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import REDIS_URL, Lab, redis_client, reset  # noqa: E402

BULK = 200_000
ORG = 100

lab = Lab(
    "07 — INVALIDATION AT SCALE",
    "One key is easy. 'Every key for org 100' is where the design shows.",
)
reset()

rdb = redis_client()


def fill(prefix: str, n: int, value: str = "x", ttl: int | None = None) -> None:
    pipe = rdb.pipeline(transaction=False)
    for i in range(n):
        pipe.set(f"{prefix}{i}", value, ex=ttl)
        if i % 10_000 == 0:
            pipe.execute()
            pipe = rdb.pipeline(transaction=False)
    pipe.execute()


class LatencyProbe:
    """A second client doing what your API does: one small GET, over and over.

    Redis executes commands on ONE thread. So this probe is not measuring
    network jitter — it is measuring how long the server spent refusing to
    serve anybody else.
    """

    def __init__(self):
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
        client.set("probe:key", "v")
        while not self._stop.is_set():
            t0 = time.perf_counter()
            client.get("probe:key")
            self.samples.append((time.perf_counter() - t0) * 1000)
            time.sleep(0.001)
        client.close()

    def __enter__(self):
        self._thread.start()
        time.sleep(0.2)  # let it establish a baseline
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def worst(self) -> float:
        return max(self.samples) if self.samples else 0.0

    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0


# ---------------------------------------------------------------------------
lab.section("The anomaly: KEYS on a single-threaded server")
lab.note(
    f"""
    "Invalidate everything for org {ORG}" has an obvious implementation:

        for key in redis.keys('catalog:v1:*:org:{ORG}:*'):
            redis.delete(key)

    It is correct. It works perfectly in staging, where the keyspace is small.
    Filling {BULK:,} keys and running it while a second client does ordinary
    GETs, which is what your API is doing at that moment.
    """
)

fill("bulk:", BULK)
lab.measure("keyspace:", f"{rdb.dbsize():,} keys, {rdb.info('memory')['used_memory_human']}")

with LatencyProbe() as probe:
    t0 = time.perf_counter()
    matched = rdb.keys("bulk:*")
    keys_ms = (time.perf_counter() - t0) * 1000
    time.sleep(0.2)
    keys_worst, keys_median = probe.worst(), probe.median()

lab.measure("KEYS bulk:* took:", f"{keys_ms:.0f}ms and returned {len(matched):,} keys")
lab.measure("meanwhile, an ordinary GET:", f"median {keys_median:.2f}ms, worst {keys_worst:.0f}ms")

lab.broke(
    keys_worst > 25,
    f"one GET waited {keys_worst:.0f}ms — {keys_worst / max(keys_median, 0.01):.0f}x the median",
    """
    Redis runs commands on one thread. KEYS scans the entire keyspace inside
    that thread, so for the whole scan NOBODY is served: not your other
    endpoints, not your other services, not the health check.

    Two costs, and people usually only think of the first:
      the scan itself blocks the server;
      the REPLY is one enormous array — allocated in the server, buffered, and
      pushed down a socket — so a big match can spike memory as well.

    In staging, with 500 keys, this returns in under a millisecond and looks
    completely fine. That is what makes it dangerous: it is not a bug you can
    find by testing, only by reasoning about the algorithm.
    """,
)


# ---------------------------------------------------------------------------
lab.section("First fix: SCAN, which is slower and never blocks")

with LatencyProbe() as probe:
    t0 = time.perf_counter()
    scanned = sum(1 for _ in rdb.scan_iter("bulk:*", count=1000))
    scan_ms = (time.perf_counter() - t0) * 1000
    time.sleep(0.2)
    scan_worst, scan_median = probe.worst(), probe.median()

lab.measure("SCAN took:", f"{scan_ms:.0f}ms for {scanned:,} keys")
lab.measure("meanwhile, an ordinary GET:", f"median {scan_median:.2f}ms, worst {scan_worst:.0f}ms")

lab.held(
    scan_worst < keys_worst / 3,
    f"worst GET during SCAN: {scan_worst:.0f}ms, versus {keys_worst:.0f}ms during KEYS",
    f"""
    Note that SCAN took LONGER overall ({scan_ms:.0f}ms vs {keys_ms:.0f}ms) and
    that is the entire point. It returns a cursor and a small batch each call,
    so other clients are served between batches. You traded total duration for
    the absence of a stall — which is the trade you want every time, because
    users experience the stall and nobody experiences the total.

    Two more habits that belong with it:
      UNLINK instead of DEL   frees big values on a background thread; DEL
                              frees them on the main one.
      SCAN guarantees         keys present the whole time are returned at least
                              once; keys added or removed during the scan may
                              or may not appear, and duplicates happen. So it
                              is fine for invalidation and wrong for anything
                              that needs an exact set.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The real fix: don't enumerate. Make the old keys unreachable.")
lab.note(
    f"""
    SCAN is still O(keyspace) to invalidate a handful of keys, still racy
    against concurrent writers, and still gets slower as you grow. The way out
    is to stop looking for the keys at all.

    Put a generation number in the key, and keep the current generation in a
    counter:

        catalog:v1:org:{ORG}:gen              → 7
        catalog:v1:g7:org:{ORG}:subject:1:datasets  → [...]
        catalog:v1:g7:org:{ORG}:subject:2:datasets  → [...]

    Invalidating the whole family is INCR. One command, O(1), no scan, no race
    — every reader immediately computes a key nobody has written yet.
    """
)

reset()
gen_key = f"catalog:v1:org:{ORG}:gen"
rdb.set(gen_key, 7)


def family_key(subject: int, gen: str) -> str:
    return f"catalog:v1:g{gen}:org:{ORG}:subject:{subject}:datasets"


gen = rdb.get(gen_key)
for subject in range(1, 6):
    rdb.set(family_key(subject, gen), f"datasets-for-{subject}", ex=300)
before = len(list(rdb.scan_iter(f"catalog:v1:g{gen}:*")))

new_gen = rdb.incr(gen_key)  # a role changed; every cached list for this org is suspect
reachable = sum(1 for s in range(1, 6) if rdb.get(family_key(s, str(new_gen))) is not None)
orphaned = len(list(rdb.scan_iter(f"catalog:v1:g{gen}:*")))

lab.measure("cached entries before:", str(before))
lab.measure("commands to invalidate all of them:", "1  (INCR)")
lab.measure("reachable after the INCR:", str(reachable))
lab.measure("orphaned, waiting on their TTL:", str(orphaned))

lab.held(
    before == 5 and reachable == 0 and orphaned == 5,
    "one INCR invalidated the entire family, in O(1), with no scan",
    """
    Same trade as the versioned keys in scenario 03, one level up: correctness
    bought with keyspace. The orphans are unreachable and expire on their own,
    and their volume is bounded by (write rate x TTL). Two consequences to say
    out loud before someone else does:

      the TTL is now load-bearing for MEMORY, not only for staleness; and
      a generation bump is a deliberate stampede — every key in the family
      misses at once, so this is scenario 06 with a trigger you control. Pair
      it with single-flight.

    The alternative, tag sets (SADD every key name into a set, then invalidate
    the set), gives you exact enumeration and a set that grows forever unless
    you prune it — you have moved the problem into a key you must now maintain.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The eviction policy that quietly deletes things you cannot recompute")
lab.note(
    """
    `maxmemory-policy allkeys-lru` is the reflex choice, and it is correct for
    a pure cache: every entry is recomputable, so evicting one costs a database
    read.

    But the same Redis usually also holds things that are NOT recomputable —
    a distributed lock, an idempotency record, a rate-limit counter, an SSE
    resume cursor. `allkeys` means exactly that.
    """
)

reset()
rdb.config_set("maxmemory-policy", "allkeys-lru")

RECORDS = 200
for i in range(RECORDS):
    rdb.set(f"orders:v1:idempotency:req-{i:04d}", f"order-{9000 + i}")  # no TTL: must not vanish
time.sleep(2.2)  # Redis's LRU clock has 1-second resolution; make these measurably older

blob = "x" * 100_000
fill("junk:", 800, value=blob, ttl=600)  # ~80MB into a 64MB server

alive = sum(
    1 for i in range(RECORDS) if rdb.get(f"orders:v1:idempotency:req-{i:04d}") is not None
)
lab.measure("idempotency records written:", str(RECORDS))
lab.measure("still there after the cache filled up:", str(alive))
lab.measure("evicted_keys:", f"{rdb.info('stats')['evicted_keys']:,}")

lab.broke(
    alive < RECORDS,
    f"{RECORDS - alive} idempotency records were evicted to make room for cache entries",
    """
    Nothing failed. No error, no log line, no metric that says "we deleted your
    idempotency records". The next retry of one of those payment requests finds
    no record and charges the customer a second time.

    And notice you cannot even say WHICH ones you lost. Redis's LRU is
    approximated by sampling a few random keys per eviction, so the survivors
    above are arbitrary. `evicted_keys` in INFO went up by a number nobody has
    an alert on.
    """,
)

reset()
rdb.config_set("maxmemory-policy", "volatile-lru")
for i in range(RECORDS):
    rdb.set(f"orders:v1:idempotency:req-{i:04d}", f"order-{9000 + i}")  # still no TTL

oom = None
for i in range(900):
    try:
        rdb.set(f"junk:{i}", blob)  # no TTL either, so nothing here is evictable
    except redis_lib.ResponseError as exc:
        oom = str(exc)
        break

alive = sum(
    1 for i in range(RECORDS) if rdb.get(f"orders:v1:idempotency:req-{i:04d}") is not None
)
lab.held(
    alive == RECORDS and oom is not None,
    f"volatile-lru kept all {alive} records — and refused the write instead",
    f"""
    The error you get is: {(oom or '')[:70]}

    That is the trade, and it is not free either: under `volatile-lru`, when
    nothing is evictable, Redis starts REFUSING WRITES. Your cache stops
    accepting entries, loudly, instead of deleting state, silently.

    Loud is better. But the real answer is not a policy at all:

      DO NOT PUT A CACHE AND A SYSTEM OF RECORD IN THE SAME REDIS.

    Different instance, or at minimum a different logical DB with its own
    memory budget and policy. A cache is sized for hit rate and may be flushed
    at any time; a lock or an idempotency record is state, and "may be flushed
    at any time" is not a property state can have. When one Redis serves both,
    the eviction policy silently decides which one you actually built.
    """,
)

rdb.config_set("maxmemory-policy", "allkeys-lru")  # leave the lab as documented
reset()

lab.takeaway(
    f"""
    "KEYS is O(keyspace) on a single-threaded server, so it stalls every other
     client for the whole scan — {keys_ms:.0f}ms on {BULK:,} keys here, with an
     ordinary GET waiting {keys_worst:.0f}ms. SCAN is slower overall and never
     blocks, so it's the one to use, with UNLINK rather than DEL. But for
     invalidating a family the real answer is not to enumerate at all: put a
     generation number in the key and INCR it — O(1), no scan, no race, at the
     cost of orphans that expire on their own and a deliberate stampede I pair
     with single-flight. And I keep caches and non-recomputable state in
     different Redis instances, because allkeys-lru will happily evict a
     distributed lock or an idempotency record to make room for a cache entry,
     and nothing anywhere will tell you it did."
    """
)

rdb.close()
lab.finish()
