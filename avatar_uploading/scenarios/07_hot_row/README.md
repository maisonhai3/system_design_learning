# The hot row — measuring the thing the review told you to remove

**The claim you are practising:** *"`users.avatar_upload_id` isn't slow because
an UPDATE is slow. It's slow because every writer to one user's row queues
behind every other writer to that row, so throughput on that row is capped at
one transaction per lock-hold, no matter how many app servers you add."*

This is the scenario that connects to *"APIs supporting 10,000 CCU — where did
it break first?"*

## Why a hot row caps throughput

Postgres takes a row-level lock for the duration of the **transaction**, not the
statement. So if your avatar-upload transaction is:

```
BEGIN;
UPDATE users SET avatar_upload_id = ?, updated_at = now() WHERE id = ?;   -- lock taken here
-- ... S3 confirmation, thumbnail record, outbox insert ...              -- lock still held
COMMIT;                                                                   -- released here
```

then the row is locked for the whole transaction — including any network call
you were unwise enough to leave inside it. Every other writer to that row waits.
Throughput for that row is `1 / transaction_duration`, and **adding application
servers makes it worse**, because each waiting request is also holding a
database connection while it waits.

That last clause is the link between this scenario and connection exhaustion.
A hot row does not just slow down the requests touching it — the waiters occupy
connections, so a single contended row can starve the pool for every *other*
endpoint. That is usually what "the database fell over at 10k CCU" actually
means.

## MVCC makes it worse than you'd guess

Postgres never updates a row in place. Every `UPDATE` writes a **new row
version** and leaves the old one for vacuum, so 2000 updates to one logical row
means 2000 physical versions. The runner proves this by watching the row's
`ctid` move and the table grow ~160 KB — from repeatedly updating a single row.

**Whether that is merely bad or genuinely awful depends on one thing: is the
column you are updating indexed?**

| Updated column | HOT updates | Cost |
|---|---|---|
| Not indexed (`updated_at`) | ~1979 / 2000 | versions chain inside the page, **no index maintenance** |
| Indexed (`avatar_upload_id`) | **0 / 2000** | every version needs its own index entries |

A *heap-only tuple* (HOT) update is the cheap path: if no index covers any
column you changed and the new version fits on the same page, Postgres chains
it in place and skips index maintenance entirely. Add an index to a column your
write path updates and you lose that optimisation completely — the runner
measures it going from 1979 HOT updates to exactly zero.

This is worth remembering as its own lesson: **adding an index to a
frequently-updated column costs you far more than the index's disk space.**

## What this scenario measures

Twenty concurrent writers, each holding its transaction open for 50ms, against
three designs:

| Design | What each writer does | Expectation |
|---|---|---|
| Hot row | `UPDATE users … WHERE id = 1` — all 20 hit the same row | serialized: ~20 × 50ms |
| Spread | `UPDATE users … WHERE id = <own>` — 20 different rows | parallel: ~50ms |
| Append | `INSERT INTO uploads …` — a new row each | parallel: ~50ms |

The gap between row 1 and row 2 is the entire cost of the design. It has
nothing to do with `UPDATE` being expensive — the same statement against
different rows is fast.

For the pgbench version with real tps numbers at `-c 1/10/50/100`, and for
connection exhaustion, see `bench/` and `./lab.sh bench`.

## The honest tradeoff

Append-only wins this benchmark, but do not oversell it. `users.avatar_upload_id`
gives you the avatar in the same row as the user — one index lookup, no join,
and it is trivially correct. Append-only needs a second query or a join to
resolve the current avatar, and the invariant lives in your read query rather
than in the schema. You are trading read simplicity for write concurrency. Say
that out loud; it is what distinguishes an engineer who measured from one who
repeated advice.

And note the cost is only real if that row is *actually* hot. One user updating
their own avatar has no contention at all. A hot row matters when many writers
converge on one row — a global counter, a per-tenant total, a leaderboard. Be
able to say which case you were in.

## Run it

    ./lab.sh run 07          # the 20-writer comparison
    ./lab.sh bench           # pgbench, real tps, -c 1/10/50/100
    ./lab.sh exhaust         # what happens at 100 clients on 50 connections

While `run 07` is going, `./lab.sh watch` in another terminal shows the waiters
piling up with `wait_event = Lock:transactionid`.

## The interview version

> "The bottleneck wasn't CPU or disk, it was one row. We had the current avatar
> as a column on `users`, so every upload updated that user's row, and the row
> lock is held for the whole transaction — throughput per row is one over
> transaction duration. I measured it: twenty concurrent writers against one
> row took twenty times as long as the same twenty against twenty rows. The
> second-order effect is what actually took the API down — each waiter holds a
> connection while it blocks, so a contended row exhausted the pool and starved
> endpoints that had nothing to do with avatars. We moved to append-only writes
> against `uploads`, which have no shared row to contend on, and paid for it
> with a slightly more complex read path."
