# Lost update — the anomaly no isolation level saves you from

**The claim you are practising:** *"Read-modify-write in application code is a
bug at any isolation level below SERIALIZABLE, and the fix is to do the
arithmetic in the database, not in Python."*

## What breaks

Two requests both want to increment a counter. Each one does what looks like
obviously correct code:

```python
value = db.query("SELECT value FROM counters WHERE name = 'page_views'")
db.execute("UPDATE counters SET value = %s WHERE name = 'page_views'", value + 1)
```

Both read `0`. Both write `1`. Two increments happened; the counter says `1`.
One update was **lost** — not rolled back, not errored, just silently gone.

The thing worth internalising: **both transactions committed successfully.**
Nothing in your logs, your error rate, or your APM will show you this. You find
out weeks later when someone reconciles the numbers.

## Why READ COMMITTED does not help

Postgres defaults to READ COMMITTED, which guarantees you never read
uncommitted data. It says nothing about whether the row changed between your
`SELECT` and your `UPDATE`. The write is perfectly legal — it just overwrites a
value your snapshot never saw.

Raising to REPEATABLE READ *does* catch this one (Postgres aborts the second
writer with `40001`), but only because both transactions touch the same row.
Do not reach for isolation levels as your first tool here; the three fixes
below are cheaper and clearer.

## Three fixes, in the order you should reach for them

| Fix | When to use it | Cost |
|---|---|---|
| `SET value = value + 1` | The new value is a pure function of the old one | Free. Always do this. |
| `SELECT … FOR UPDATE` | You must compute the new value in app code | A row lock held for the rest of the transaction |
| `version` column + `WHERE version = ?` | Long-lived reads; user-facing "someone else edited this" | Retry loop in app code |

The first is not a lock — it is a single statement, so Postgres takes and
releases the row lock itself. Reach for it first every time.

## Run it

    ./lab.sh run 01          # automated proof
    ./lab.sh a 01            # terminal 1
    ./lab.sh b 01            # terminal 2

## The interview version

> "Any read-modify-write across two round trips is a lost-update race. We hit
> it on a counter. The fix wasn't a lock, it was moving the arithmetic into the
> statement — `SET value = value + 1` — so the database does the read and the
> write atomically. Where we genuinely had to compute in app code, we took
> `SELECT FOR UPDATE` on the row first, and for the long-lived editing screens
> we used an optimistic version column so a stale save fails loudly instead of
> silently clobbering."
