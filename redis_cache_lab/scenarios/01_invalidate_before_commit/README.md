# Invalidate before COMMIT — the ordering that poisons a cache forever

**The claim you are practising:** *"Delete the cache entry after the transaction
commits, never before. Before the commit, the database still serves the old row,
so any concurrent miss re-caches the value you just deleted — and a cache hit
never re-reads the database, so that wrong value is self-sustaining."*

## The reasoning that gets it backwards

> If I delete the cache first and commit second, the window where the cache
> disagrees with the database is smaller.

Everyone arrives at that, and it is not a stupid thought — it is the right
instinct applied to the wrong model. It assumes the danger in the window is
*someone reading the cache*. The danger is someone **writing** it.

## What actually happens

```
W: BEGIN
W: UPDATE users SET role = 'admin' WHERE id = 1     -- uncommitted
W: DEL identity:v1:user:1:profile                    -- cache now empty
                          R: GET  ... → miss
                          R: SELECT role ... → 'guest'   ← still the old row!
                          R: SET  ... 'guest'            ← after the DEL
W: COMMIT
```

Final state: **Postgres says `admin`, Redis says `guest`.** Both transactions
committed. Both requests returned 200. Nothing was logged.

And it does not heal, because the next read is a *hit* — and a hit never
consults the database. The wrong value keeps itself alive. Without a TTL it
lives until a human notices, which for a permissions cache means until someone
gets access they should not have.

## Why the window is bigger than you think

The window is not "the microseconds between two lines of Python". It is
**everything left in the transaction after the DEL**:

| What else is in that transaction | Window |
|---|---|
| Nothing, single UPDATE | ~1ms |
| Four more statements, one with a FK check | 5–20ms |
| A `SELECT ... FOR UPDATE` that queues behind another writer | as long as that writer takes |
| A transaction that opened before an HTTP call to another service | seconds |

Every practice that makes transactions slower — bigger transactions, remote
calls inside them, hot-row contention — widens this bug's window. That is why
it shows up in load tests and never on your laptop.

## The fix, and what it does not fix

Move one line:

```python
async with uow:                       # BEGIN
    await repo.update_role(...)       # UPDATE
                                      # COMMIT (on exiting the block)
await cache.delete(user_key(user_id)) # ← only now
```

The stale window still exists — between COMMIT and DEL a reader can hit the old
entry — but it is **bounded** by the gap between the two statements, and it
**closes by itself**. That is the entire trade, and it is worth saying in exactly
those words:

> You do not get to eliminate the inconsistent window. You only get to choose
> whether it closes by itself.

This does **not** make the cache correct. A reader that started before the
commit and finished after the delete poisons it the same way, and no reordering
of two statements can prevent that. See scenario 03 — the one that separates
reciting the standard answer from understanding it.

## Run it

```bash
./lab.sh run 01
```

Watch it happen live from a second terminal:

```bash
./lab.sh events     # every SET/DEL as it lands
./lab.sh spy        # every command Redis receives
```

## The interview answer

> *"Where do you invalidate the cache — before or after the commit?"*

"After, always. Before the commit, the row in Postgres is still the old value
for everyone except my own transaction, so a concurrent cache miss reads the old
value and writes it back after my DEL. That leaves the cache permanently wrong
with no error anywhere, because subsequent reads are hits and never re-check the
database.

After the commit I still have a stale window between COMMIT and DEL, but it is
bounded and self-healing. I'd add a TTL as a backstop so a lost DEL can't make
it permanent, and if the data is a permission decision I'd use a version-keyed
entry instead of a delete, because delete-after-commit still loses to a slow
reader that started before my commit."

That last sentence is what makes the answer sound like experience rather than a
blog post — and it is scenarios 02 and 03.
