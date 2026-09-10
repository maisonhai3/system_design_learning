# Redis Cache Lab — a cache you can break on purpose

A throwaway Redis and Postgres, built around one question that sounds settled
and isn't:

> **When do you delete the cache entry — before the commit, or after?**

The usual answer ("after") is correct and incomplete. Chasing it properly
takes you through every Redis failure worth knowing: the invalidation that
poisons the cache permanently, the invalidation that never ran, the race that
correct ordering does *not* fix, the key that leaks one user's rows to another,
the herd that arrives one microsecond after an eviction, and the `KEYS` command
that stalls a production cluster.

Every scenario ships as an **automated proof**: it forces the interleaving,
asserts the anomaly actually reproduced, asserts the fix actually held, and
exits non-zero if either claim stops being true. Then there is a **FastAPI
service** you can break by hand with two `curl`s.

## Quick start

```bash
./lab.sh up          # redis on :6380, postgres on :5436, schema + seed applied
./lab.sh list        # what's in here
./lab.sh read 03     # the scenario this lab exists for
./lab.sh run 03      # watch it break, then watch three fixes
./lab.sh run-all     # every proof; non-zero if any claim stops holding
```

Play with it by hand:

```bash
./lab.sh app         # FastAPI on :8001, docs at /docs
./lab.sh demo        # drives the scenario 03 race over HTTP, in two requests
```

Watch what Redis is actually doing, from a second terminal:

```bash
./lab.sh events      # every SET / DEL / EXPIRE / eviction as it lands
./lab.sh spy         # every command the server receives (MONITOR)
./lab.sh keys        # the keyspace with TTLs — read ttl=-1 as "wrong forever"
./lab.sh dblog       # every query Postgres ran = your cache-miss log
```

## The scenarios

| # | Scenario | The claim you'll be able to defend |
|---|---|---|
| 01 | Invalidate before COMMIT | Deleting before the commit re-caches the value you just deleted, and a cache hit never re-reads the database, so it is self-sustaining. |
| 02 | The lost DELETE | `BackgroundTasks` is in-memory. TTL is not a memory setting — it is how long you may be wrong. The durable version is an outbox. |
| 03 | **The read-repopulate race** | The one delete-after-commit doesn't fix. The last writer to Redis wins, and cache-aside lets the *stale reader* be last. |
| 04 | Key strategy | `service:v1:entity:id:projection` — every segment prevents a named failure. Measured: **610 MB** of RAM difference at 10M keys, in names alone. |
| 05 | **The permission cache** | A key missing the subject is a data leak, not a risk of one. Your TTL *is* your revocation latency. Cache the inputs, not the decisions. |
| 06 | The stampede | Measured: 40 concurrent requests → **40 database reads**, or **1** with single-flight. A load fix, not a latency fix. |
| 07 | Invalidation at scale | Measured: `KEYS` on 200k keys stalls an ordinary `GET` for **90 ms**. The real fix is a generation counter, not a better scan. |

Every number above was measured by the runners on the machine that built this.
`./lab.sh run-all` re-measures them on yours.

## The playground

`./lab.sh app` starts a small FastAPI service written the way the job
description asks for — Clean Architecture, SQLAlchemy 2.0 async, `Depends()`
wiring, `BackgroundTasks` for the invalidation — with switches that let you turn
each bug on.

```bash
curl -si localhost:8001/users/1 | grep -i x-cache          # MISS, then HIT

# scenario 01, by hand
curl -X PUT 'localhost:8001/users/1/role?role=admin&mode=del_before_commit'

# scenario 05, by hand: watch Bob get served Alice's payroll dataset
curl -s -XPOST localhost:8001/debug/flush
curl -s -H 'X-User-Id: 1' 'localhost:8001/datasets?leaky_key=true'
curl -s -H 'X-User-Id: 2' 'localhost:8001/datasets?leaky_key=true'

# scenario 02, by hand: a key with no expiry
curl -s 'localhost:8001/users/1?ttl='
curl -s localhost:8001/debug/keys                          # ttl: -1
```

`mode=` on the write path is `del_before_commit` | `del_after_commit` |
`versioned` | `no_invalidate`. Two of those are bugs, on purpose.

## Layout

```
lab.sh                  one entry point for everything
docker-compose.yml      redis:7 on :6380, postgres:16 on :5436 — mis-tuned for teaching
schema/                 01_schema.sql, 02_seed.sql — idempotent, re-run on reset
scenarios/NN_name/
    README.md           what breaks, why, the fixes, and the interview answer
    run.py              the automated proof (uv runs it; no venv to manage)
lab/harness.py          session/trace/latch/assertion plumbing
lab/cache.py            the cache-aside strategies under test, side by side
app/
    domain/             entities + Protocols. No framework imports at all.
    usecases/           business logic. Depends only on the Protocols.
    adapters/           SQLAlchemy async, redis.asyncio. The only I/O.
    api/                routers + Depends() wiring. The only FastAPI imports.
    demo.sh             drives the scenario 03 race over HTTP
```

The architecture has a grep test, and it is the inner layers that have to pass it:

```bash
grep -rn "fastapi\|sqlalchemy\|redis" app/domain app/usecases --include='*.py'
```

One docstring mention, no imports. `app/main.py` and `app/api/` are allowed to
know about FastAPI — they are the composition root. Nothing below them is.

## How the proofs work

Each `run.py` asserts **two** kinds of claim, and the run fails if either stops
holding:

- `ANOMALY REPRODUCED` — the naive version really did break
- `FIX HELD` — the fix really did work

The first matters more than it looks. A teaching lab that quietly stops
demonstrating its own bug — because a library changed, or a fix was too eager —
is worse than no lab, because you would walk into the interview still believing
it. `./lab.sh run-all` exits non-zero the moment that happens.

Output is a trace attributed to each actor, with Redis and Postgres interleaved,
so you can read the incident rather than the code:

```
  R │ GET identity:v1:user:1:profile  → None
  R │ SELECT role FROM users WHERE id = 1  → 'guest'
  R │ # … paused, holding a value that is already going stale
  W │ UPDATE users SET role = 'admin' WHERE id = 1  → 1 row(s) affected
  W │ COMMIT
  W │ DEL identity:v1:user:1:profile  → 0          ← deleted nothing
  R │ SETEX identity:v1:user:1:profile 300 'guest' ← poisoned
```

### On proving a race without `sleep()`

`time.sleep()` does not prove a race. It makes one *likely*, which is the same as
making a test flaky, and a flaky test is a test that gets deleted.

The scenarios force the interleaving with `Latch` — a named pause point inside
the code under test that blocks until the test lets it through. The race becomes
a sequence, the sequence is deterministic, and the proof is a proof. The honest
cost is that the seam lives in the code under test; the alternatives are a
Postgres advisory lock held by the test, an injected clock, or a fake Redis whose
`SET` blocks on command. The `app/` service takes the third option's cousin — a
config-gated `?stall_ms` on the chaos path — which is how `./lab.sh demo` can
reproduce scenario 03 with two ordinary HTTP requests.

## Where this lab disagrees with the conversation that prompted it

Two corrections worth having in your head before an interview:

**1. The API is `add_task`, not `add`.**

```python
background_tasks.add_task(cache.delete, key)   # Starlette's actual signature
```

**2. "Keep `BackgroundTasks` in the router" is right, and incomplete.** The
router should own the *scheduling* — it is the only layer with a request
lifecycle. But if the router also decides *which keys* to delete, then every
caller of that use case has to know the cache layout: the HTTP route, the
cronjob, the Kafka consumer, the admin command. One of them will forget, and the
bug it produces is scenario 01 with a different entry point.

So the use case returns an `Invalidation` — what must be invalidated, which is
domain knowledge — and the caller decides how to carry it out: deferred via
`BackgroundTasks` in a request, inline in a batch job, through an outbox when it
must not be lost. Knowledge in the layer that owns it, scheduling in the layer
that has a scheduler. `app/usecases/update_role.py` and `app/api/routers.py`
show the split.

## The companion lab

`../realtime_gateway_lab` reuses this lab's domain — the same Alice and Bob, the
same datasets, the same one grant row — and asks the other half of the question.
This lab is about what it costs to **cache** an authorization answer. That one is
about what it costs to **push** one, and who is allowed to decide it: Redis
Streams and SSE resume, fan-out on write, and a real Traefik + nginx gateway you
can bypass on purpose.

## The interview questions this is built for

**"You cache a user's permissions in Redis. Walk me through it."**
Scenarios 01 → 02 → 03 → 05, in that order. The answer that lands is not "cache
aside with a TTL"; it is knowing that delete-after-commit is a *mitigation*, that
the TTL is the revocation latency, and that a key missing the subject is a leak
rather than a risk.

**"How would you invalidate every cached list for a tenant?"**
Scenario 07. `KEYS` is the trap, `SCAN` is the improvement, and the answer is a
generation counter — plus knowing that bumping it is a deliberate stampede, which
is scenario 06.

**"Write me a test that proves a race condition."**
The `Latch` in `lab/harness.py` and any of scenarios 01, 03 or 06. The point to
make out loud: you cannot prove ordering by sleeping, you have to control it.

## Notes

**Ports 6380 and 5436, not 6379 and 5432.** The other labs in this repo already
use 6379, 5432, 5433, 5434 and 5435. This one never touches them.

**The server is mis-tuned on purpose.** `maxmemory 64mb` so a laptop can fill it,
`allkeys-lru` because it is the reflex choice scenario 07 argues against,
`notify-keyspace-events KEA` so `./lab.sh events` works, and
`log_min_duration_statement=0` on Postgres so every cache miss shows up in
`./lab.sh dblog`. Do not copy these anywhere real.

**Reset freely.** `./lab.sh reset` re-applies schema and seed and flushes Redis in
about a second; every `run.py` calls it first, so scenarios never contaminate each
other. `./lab.sh down` deletes both containers and their volumes.

**No Alembic here, deliberately.** The schema is owned by `schema/*.sql` because
the lab has to reset it in a second and the scenarios drive it with raw psycopg.
This repo's `student_course_enrollment` lab has the Alembic setup this one skips.

**Requirements:** Docker, `psql` and `redis-cli`, and
[`uv`](https://docs.astral.sh/uv/) for the runners and the app — which fetch
their own dependencies via inline script metadata, so there is no virtualenv to
manage.

**Verified.** Every scenario, `./lab.sh run-all` (32/32 claims), the FastAPI app
and `./lab.sh demo` were run end to end on Docker with `redis:7-alpine` and
`postgres:16` while this was written. The measured numbers in the tables above
came from those runs.
