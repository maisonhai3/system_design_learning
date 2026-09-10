# Invalidation at scale — one key is easy, "every key for org 100" is a design

**The claim you are practising:** *"`KEYS` is O(keyspace) on a single-threaded
server, so it stalls every other client for the whole scan. `SCAN` is slower
overall and never blocks. But for invalidating a family, the real answer is not
to enumerate at all."*

## Measured on this machine, 200,000 keys

```
KEYS bulk:*    took 342ms   → an ordinary GET waited  90ms  (255x the median)
SCAN bulk:*    took 498ms   → an ordinary GET waited   4ms
```

Read those two rows together. **`SCAN` took longer and that is the entire
point.** It returns a cursor and a small batch per call, so other clients are
served between batches. You trade total duration for the absence of a stall —
the right trade every time, because users experience the stall and nobody
experiences the total.

## Why `KEYS` is so bad

Redis executes commands on **one thread**. `KEYS` scans the whole keyspace inside
that thread, so for the entire scan *nobody* is served: not your other endpoints,
not your other services, not the health check.

Two costs, and people usually think of only the first:

1. the scan blocks the server;
2. the **reply** is one enormous array — allocated server-side, buffered, pushed
   down a socket — so a big match spikes memory too.

In staging, with 500 keys, this returns in under a millisecond and looks
completely fine. That is what makes it dangerous: **you cannot find this by
testing, only by reasoning about the algorithm.**

Two habits that belong with `SCAN`:

- **`UNLINK` instead of `DEL`** — frees large values on a background thread; `DEL`
  frees them on the main one.
- **Know `SCAN`'s guarantees** — keys present for the whole scan are returned at
  least once; keys added or removed *during* the scan may or may not appear, and
  duplicates happen. Fine for invalidation, wrong for anything needing an exact
  set.

## The real fix: make the old keys unreachable

`SCAN` is still O(keyspace) to invalidate a handful of keys, still racy against
concurrent writers, and still gets slower as you grow. Stop looking for the keys.

```
catalog:v1:org:100:gen                       → 7        the generation counter
catalog:v1:g7:org:100:subject:1:datasets     → [...]
catalog:v1:g7:org:100:subject:2:datasets     → [...]
```

Invalidating the entire family is `INCR catalog:v1:org:100:gen`. **One command,
O(1), no scan, no race** — every reader immediately computes a key nobody has
written yet.

| | commands | complexity | racy |
|---|---:|---|---|
| `KEYS` + `DEL` | 1 + N | O(keyspace), blocking | yes |
| `SCAN` + `UNLINK` | N/batch | O(keyspace), non-blocking | yes |
| **`INCR` a generation** | **1** | **O(1)** | **no** |

Same trade as the versioned keys in scenario 03, one level up: correctness bought
with keyspace. Two consequences to say out loud before someone else does:

- The orphans expire on their own, bounded by *write rate × TTL* — so **the TTL is
  now load-bearing for memory**, not only for staleness.
- A generation bump is a **deliberate stampede**: every key in the family misses
  at once. That is scenario 06 with a trigger you control, so pair it with
  single-flight.

The alternative — **tag sets** (`SADD` every key name into a set, invalidate the
set) — gives you exact enumeration and a set that grows forever unless you prune
it. You have moved the problem into a key you must now maintain.

## The eviction policy that deletes things you cannot recompute

`maxmemory-policy allkeys-lru` is the reflex choice, and it is correct for a
*pure* cache: every entry is recomputable, so an eviction costs one database
read.

But the same Redis usually also holds things that are **not** recomputable — a
distributed lock, an idempotency record, a rate-limit counter, an SSE resume
cursor. `allkeys` means exactly that:

```
idempotency records written:            200
still there after the cache filled up:    8
evicted_keys:                         1,088
```

Nothing failed. No error, no log line, no metric saying "we deleted your
idempotency records". The next retry of one of those payment requests finds no
record and charges the customer a second time.

And you cannot even say *which* ones you lost: Redis's LRU is approximated by
sampling a few random keys per eviction, so the survivors are arbitrary.

### `volatile-lru` is better and still not the answer

Under `volatile-lru`, keys with no TTL are never evicted — the records all
survive. But when nothing is evictable, **Redis starts refusing writes**:

```
OOM command not allowed when used memory > 'maxmemory'
```

Your cache stops accepting entries, loudly, instead of deleting state, silently.
Loud is better. But the real answer is not a policy at all:

> **Do not put a cache and a system of record in the same Redis.**

Different instance, or at minimum a different logical DB with its own memory
budget and policy. A cache is sized for hit rate and may be flushed at any time; a
lock or an idempotency record is *state*, and "may be flushed at any time" is not
a property state can have. When one Redis serves both, **the eviction policy
silently decides which one you actually built.**

## Run it

```bash
./lab.sh run 07
./lab.sh status         # keyspace_hits / misses / evicted_keys
./lab.sh cli --latency  # live latency against the lab
```

## The interview answer

> *"How do you invalidate every cached list for a tenant?"*

"Not with `KEYS` — it's O(keyspace) on a single-threaded server, so it stalls
every other client for the whole scan; here it was 342ms on 200k keys, with an
ordinary GET waiting 90ms. `SCAN` is slower overall and never blocks, with
`UNLINK` rather than `DEL`, so that's the one to reach for.

But for a whole family the real answer is not to enumerate at all: put a
generation number in the key and `INCR` it. O(1), no scan, no race. The cost is
orphaned keys that expire on their own, and a deliberate stampede at the moment
of the bump — so I pair it with single-flight.

And I keep caches and non-recomputable state in separate Redis instances, because
`allkeys-lru` will happily evict a distributed lock or an idempotency record to
make room for a cache entry, and nothing anywhere will tell you it did."
