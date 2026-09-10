# The lost DELETE — why TTL is not optional

**The claim you are practising:** *"`BackgroundTasks` is in-process and
in-memory. If the pod dies between the commit and the DEL, the write is durable
and its invalidation is not. A TTL is the bound on how long the system may be
wrong when that happens — and for a permission cache that number is a security
parameter, not a memory setting."*

## The setup is already correct

Scenario 01's fix is in place: commit first, then delete. The delete is handed
to `BackgroundTasks` so the client is not made to wait for a Redis round trip.

```python
@router.put("/users/{user_id}/role")
async def update_role(user_id: int, body: RoleIn,
                      background_tasks: BackgroundTasks,
                      use_case: UpdateRole = Depends(get_update_role)):
    await use_case(user_id, body.role)                 # commits
    background_tasks.add_task(cache.delete, user_key(user_id))
    return {"ok": True}
```

This is the right shape. It also has three ways to lose the DEL.

## Three ways, all of which look like success to the caller

| How | What the caller sees | What your metrics see |
|---|---|---|
| Pod evicted / OOM / deploy between response and task | 200 | nothing |
| Redis unreachable when the task runs | 200 | nothing — the response already left |
| The task raises for any other reason | 200 | a server-side log nobody alerts on |

The response has already been sent by the time a background task runs. So none
of these appear in your API error rate. They appear weeks later as *"a user says
they still see the old permissions"*, with nothing in the logs to connect it to.

The asymmetry is the whole bug: **you made one system's write durable and left
the other system's write in a Python list.**

## Backstop 1: a TTL

```python
await redis.set(key, value, ex=300)   # not redis.set(key, value)
```

A TTL does not prevent staleness. It converts *unbounded* staleness into a
number you chose on purpose. That reframing is the point — the question "what
TTL?" is really:

> **How long is this system allowed to be wrong?**

| TTL | What you are actually saying |
|---|---|
| 24h | "A revoked admin keeps their access until tomorrow." |
| 5m | The usual default. Five minutes of wrongness, worst case. |
| 30s | A permission cache you can defend in a security review. |
| none | "Forever", chosen by accident. |
| 0 (no cache) | Sometimes right, and always the baseline to measure against. |

If you cannot answer the second question, you were not entitled to pick the
first number.

Two things to add once the TTL exists:

- **Jitter.** `ex=300 + random.randint(0, 60)`. A thousand keys written in the
  same second expire in the same second, and you get a synchronised stampede —
  scenario 06.
- **Different TTLs per projection.** `:profile` can be minutes. `:permissions`
  should be seconds. One TTL for everything means the least tolerant consumer
  sets the number for all of them.

## Backstop 2: make the invalidation as durable as the write

You cannot make Postgres and Redis atomic. You can record the *intent* to
invalidate in the same transaction as the data change:

```sql
BEGIN;
UPDATE users SET role = 'admin' WHERE id = 1;
INSERT INTO cache_invalidations (cache_key) VALUES ('identity:v1:user:1:profile');
COMMIT;
```

Now there is no interleaving where the row changed and the invalidation was
forgotten. A relay drains the table and retries until Redis accepts:

```sql
SELECT id, cache_key FROM cache_invalidations
WHERE drained_at IS NULL ORDER BY id
FOR UPDATE SKIP LOCKED;      -- so N relay replicas don't fight over a row
```

This is the transactional outbox — the same pattern this repo's
`student_course_enrollment` lab uses for publishing messages, for the same
reason: two systems cannot be made atomic, so one of them becomes the record of
intent for the other.

**What it does not buy you:** it is not faster and it is not synchronous. The
cache is stale for as long as the relay lag. What changes is that the staleness
is bounded by a lag you can *measure and alarm on*, instead of a TTL you
guessed.

**What it costs:** a table, a process, and a backlog that can grow. For a
profile cache that is over-engineering. For a permission cache where the failure
mode is "still has access", it is the difference between a bounded and an
unbounded risk.

## The alternatives, in one line each

- **`LISTEN`/`NOTIFY`** — no extra table, but delivery is at-most-once: a
  listener that was disconnected misses the notification and never learns it did.
- **CDC (Debezium et al.)** — reads the WAL, so no writer can bypass it, not even
  `psql` at 2am. The heaviest to operate, and the only one that is airtight.
- **TTL alone** — no moving parts. Perfectly respectable, once you have said out
  loud how long you are willing to be wrong.

## Run it

```bash
./lab.sh run 02
```

## The interview answer

> *"You invalidate the cache in a background task. What happens if it doesn't
> run?"*

"Then the row is updated in Postgres and the cache still serves the old value,
and because the response was already returned, nothing in my error rate shows
it. So I never rely on the DEL alone. Every entry gets a TTL, and I treat the
TTL as the maximum time the system is allowed to be wrong — seconds for
permissions, minutes for profile data, with jitter so they don't all expire
together. If losing an invalidation is genuinely unacceptable, I write the
invalidation into the same transaction as the data change and have a relay drain
it, so it's exactly as durable as the write it belongs to."
