# The connection budget — twelve idle browser tabs took down an unrelated endpoint

**The claim you are practising:** *"A long-lived connection is a held resource
for as long as it is open. The question stops being requests-per-second and
becomes: what does each connection hold?"*

## Measured on this machine

```
GET /datasets, idle server:                        9ms
GET /datasets, with 12 SSE streams holding a session:  6000ms  (timed out)
GET /datasets, with 12 SSE streams done properly:      8ms
```

Pool size is 10. Twelve people opened a feed and left it open — no events, no
queries, no CPU — and an endpoint that never touches the feed stopped answering.

## The bug is a default, not a mistake

```python
@router.get("/feed")
async def feed(session: AsyncSession = Depends(get_session)):
    return StreamingResponse(...)
```

FastAPI acquires the session when the request starts and releases it when the
**response ends**. For a normal endpoint that is milliseconds. For SSE it is
however long the user leaves the tab open.

Nobody wrote a bug. Somebody used the dependency that works everywhere else.

## Three properties that make it hard to diagnose

1. **It scales with idle users, not with traffic.** Your requests-per-second
   graph is flat while everything gets worse.
2. **It appears at the size of the pool** — a number nobody thinks of as a user
   limit.
3. **The endpoint that breaks is never the one you changed.**

And it does not stop at the pool. Each of those sessions holds a **transaction**
open, so `DROP TABLE` in your next migration queues behind a browser tab in
another timezone, and your deploy hangs.

## The fix is a type signature, not a tuning knob

```python
class StreamFeed:
    def __init__(self, datasets: DatasetRepositoryFactory, log: EventLog): ...

    async with self.datasets() as datasets:      # microseconds
        dataset = await datasets.get(...)
```

The connection is held for the duration of a **query**, not the duration of a
**connection**. That is the whole distinction.

Raising `pool_size` is the tempting alternative, and it just moves the number: it
converts "breaks at 10 users" into "breaks at 100 users" while multiplying your
idle Postgres backends. The resource is not scarce because the pool is small; it
is scarce because you are holding it for hours.

Note that in the default read mode there is **no query at all** — fan-out already
decided who may see the event, so the read path never touches Postgres. Scenario
02's design choice pays for itself again here.

## What one idle connection actually costs

```
file descriptors with 12 streams:   48
file descriptors after they close:  24
per connection:                     ~2.0 fds
```

The full inventory, none of which a requests-per-second graph shows you:

| held | size |
|---|---|
| a socket and its kernel buffers | ~10–60 KB, plus an fd against the process limit — often **1024** in a base image, which is your real concurrency ceiling |
| an asyncio task and its generator | small, but never collected while the client is connected |
| a Redis connection blocked in `XREAD` | one per stream, against Redis's `maxclients` (default 10,000) |
| a pooled database session | **zero**, if you got the factory right |
| a slot in every proxy in the path | Traefik, the ingress, the load balancer — each with its own limit |

**Capacity planning for a push feed is "concurrent connections", and it is bounded
by the smallest of those five numbers — usually one you have never looked at.**

## What to do, in order

1. **Hold nothing per connection that you can acquire per operation.** Sessions,
   transactions, file handles, locks. Free, and the whole fix for most services.
2. **Cap concurrency explicitly, per subject.** A rate limiter counts requests and
   one SSE request is a thousand events, so it does not help here. You need "at
   most N streams per user" — which also stops one person with fifty tabs from
   being your capacity problem.
3. **Size the pieces against connections, not requests.** fd limits, Redis
   `maxclients`, proxy connection limits, worker counts.
4. **Then ask whether you needed a push channel at all.** Ten thousand idle SSE
   connections is real infrastructure. A thirty-second poll against a cached
   endpoint is a cron job, costs nothing to operate, and for plenty of "realtime"
   features is indistinguishable to the user. Choosing SSE should be a decision,
   not a default.

This is the same lesson as this repo's `avatar_uploading` lab, arriving from the
other direction: there, a contended row exhausts the pool because every waiter
holds a connection; here, an idle stream does it because nobody ever lets go.

## Run it

```bash
./lab.sh run 06

# see it by hand — open several of these, then try /datasets
./lab.sh sse alice "hold_session=true&block_ms=30000"
./lab.sh as alice /datasets
```

## The interview answer

> *"You're running SSE feeds for 10,000 users. What breaks first?"*

"Whatever each connection holds for its whole lifetime. The one that bites is a
request-scoped database session: FastAPI releases it when the response ends, and
an SSE response ends when the user closes the tab — so in this lab 12 idle
browser tabs exhausted a pool of 10 and starved an endpoint with nothing to do
with the feed. It scales with idle users rather than traffic, so nothing on the
traffic graph explains it, and the open transactions also block the next
migration's DDL.

The fix is a type signature: the streaming use case takes a repository *factory*
and opens a session only while it queries. After that, capacity is bounded by
whichever is smallest — file descriptors, Redis `maxclients`, or a proxy's
connection limit — and I cap concurrent streams per subject, because a rate
limiter counts requests and one stream is one request."
