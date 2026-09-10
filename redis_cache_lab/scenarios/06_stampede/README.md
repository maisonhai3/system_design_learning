# The stampede — a cache protects the database until the moment it doesn't

**The claim you are practising:** *"A cache protects the database at steady
state and stops protecting it at exactly the moment a key is invalidated. Every
in-flight request misses at once and they all query the same row."*

## Measured on this machine

```
                requests   database reads   wall clock
naive                  40               40        520ms
single-flight          40                1        317ms
                                    ────────
                             40x less load
```

Every one of those 40 requests returned the correct answer. Nothing was
inconsistent. The database simply received 40× the load it was sized for, in a
burst, at the worst possible moment — right after a write.

## Why it is worse than 40×

The failure is **self-reinforcing**. The 40 concurrent queries make the database
slower; a slower query widens the window in which new arrivals also miss; more
arrivals means more queries. A 99% hit rate does not protect you, because this
is entirely about the 1%.

It is also why *"we added a cache and now the database falls over during
deploys"* is a real sentence. A deploy restarts every pod at once, so every key
goes cold simultaneously.

## Single-flight

```python
token = uuid4().hex
if await redis.set(lock_key, token, nx=True, px=3000):   # I am the winner
    try:
        value = await load_from_db()
        await redis.set(key, value, ex=300)
    finally:
        await release(lock_key, token)                   # compare-and-delete
else:
    value = await wait_for(key)                          # losers wait
```

`SET key value NX PX ms` is the whole mechanism: atomic test-and-set with a
self-healing expiry.

**Wall clock barely moved** (520ms → 317ms), and that is the point worth
noticing: single-flight is not a latency optimisation, it is a **load**
optimisation — and load is what takes the database down.

The tradeoff you are choosing: 39 requests now wait on someone else's query
instead of running their own. For a read the user is blocked on, that is right.
For a dashboard, serving the stale value while one request refreshes in the
background is better. **Blocking versus stale-while-revalidate is a product
decision, not a Redis one.**

## Two details in the lock that are not optional

### 1. `PX` — the lock must expire

Without a TTL, a holder that crashes wedges the key forever. Forever is a long
time.

### 2. The release must be compare-and-delete

```
A: SET lock tokenA NX PX 100      A holds it
   ... A's query takes 150ms (GC pause, slow plan, network stall)
   ... the lock expires
B: SET lock tokenB NX PX 3000     B now holds it
A: DEL lock                       ← A just released B's lock
```

There is now no lock at all while B is still working. Two leaders — and the
failure looks exactly like the bug you added the lock to fix.

Worse: the lock is not merely useless, it is **actively misleading**. The
incident review will start by ruling out the lock, because "we take a lock
there".

```lua
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
```

It has to be a script for the same reason as everything else in this lab: two
round trips have a gap, and the gap is where the bug lives.

**The honest caveat:** this is single-instance mutual exclusion. Across a
failover it is not safe, because the new primary may not have the lock. Redlock
exists and is contested. If *correctness* — not just load — depends on mutual
exclusion, use a Postgres advisory lock or a unique constraint. That is what
this repo's `avatar_uploading` lab is about.

## The synchronised expiry you built yourself

A deploy warms 10,000 keys in one second, all with `ex=300`. Five minutes later
they all expire in the same second, and you get this stampede across every key
at once, on a five-minute cycle, forever.

Measured on 400 keys with a 1-second TTL:

```
fixed ttl=1000ms,       alive after 1.3s:    0 of 400
jittered 1000-3000ms,   alive after 1.3s:  317 of 400
```

One line fixes it:

```python
ttl = base + random.randint(0, base // 5)
```

Do it everywhere you set a TTL, and especially in the code that warms the cache
at startup — that is the one that creates ten thousand keys inside one second.

## The other two tools, so you can name them

- **stale-while-revalidate** — serve the expired value, refresh in the
  background. Zero user-visible latency, at the cost of a deliberately stale
  response. Needs a second, harder expiry so "stale" cannot become "forever".
- **probabilistic early expiry (XFetch)** — each reader recomputes with a
  probability that rises as the TTL approaches, so one reader refreshes early
  and the rest keep hitting. No lock, no coordination. Elegant, and harder to
  explain in an incident review at 3am — which is a real engineering cost, not a
  joke.

## Run it

```bash
./lab.sh run 06
./lab.sh dblog          # the SELECTs Postgres actually received
```

## The interview answer

> *"Your cache has a 99% hit rate. Is the database safe?"*

"Not during the 1%. When a hot key is invalidated — by a write, a TTL, or an
eviction — every in-flight request for it misses at the same instant and they all
run the same query. In this lab 40 concurrent requests became 40 database reads;
with single-flight it was 1. It's a load fix, not a latency fix: wall clock
barely changed.

The lock needs a TTL so a crashed holder can't wedge the key, and the release has
to be a compare-and-delete in Lua, or a holder whose work overran frees somebody
else's lock. And I jitter every TTL, because ten thousand keys warmed in the same
second expire in the same second."
