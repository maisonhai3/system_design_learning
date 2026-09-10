# The read-repopulate race — the one delete-after-commit doesn't fix

**The claim you are practising:** *"Delete-after-commit fixes the common case,
not the race. A reader that SELECTed before my commit and SETs after my delete
re-poisons the cache — and the DEL returns 0, so the writer never learns it
achieved nothing."*

This is the scenario that separates reciting the standard answer from
understanding why it is only a mitigation. If you only run one, run this one.

## The interleaving

Everything the writer does is correct. It still loses.

```
R: GET  identity:v1:user:1:profile      → miss
R: SELECT role FROM users WHERE id = 1  → 'guest'     ← correct at this instant
                    W: BEGIN / UPDATE role='admin' / COMMIT
                    W: DEL identity:v1:user:1:profile → 0    ← deleted nothing!
R: SET  identity:v1:user:1:profile 'guest'            ← lands last. Poisoned.
```

Look at the `→ 0` on the writer's DEL. There was nothing in the cache to delete
at that moment, because the reader had not written it yet. The writer did
everything by the book, got a successful response, and accomplished nothing.

## The sentence worth memorising

> **The last writer to Redis wins, and cache-aside hands the stale reader a
> chance to be last.**

Once you see it that way, every fix falls into exactly two families:

| Family | How | Verdict |
|---|---|---|
| (a) Stop the stale reader being last | delayed double delete, distributed read lock | probabilistic, or expensive |
| (b) Make being last not matter | put the row version in the key | deterministic, nearly free |

Nothing else exists. That taxonomy is worth more in an interview than any
individual trick, because it tells you what to do with a trick you have never
seen before.

## (a) The delayed double delete

Delete after the commit, then delete again ~500ms later:

```python
await cache.delete(key)                     # may delete nothing
background_tasks.add_task(delete_later, key, after=0.5)
```

The lab proves it repairs this exact interleaving. Now say the caveat out loud
before an interviewer says it for you:

> The delay has to exceed the longest possible gap between a reader's SELECT and
> its SET. That gap includes a GC pause, a slow query, a network hiccup, and a
> thread that lost its CPU slice. **Nobody can size that number.**

It lowers the probability. It does not close the race. It also doubles your
invalidation traffic and needs a timer that survives the process — which is
scenario 02 all over again. Know it, name it as a mitigation, don't defend it as
a fix.

## (b) Put the version in the key

```
identity:v1:user:1:profile     → "7"          the pointer: which version is current
identity:v1:user:1:profile@7   → "admin"      the value, at that version
```

- The reader gets `role` **and** `version` from the same SELECT — no extra round
  trip to Postgres.
- It writes the value under the version it actually read: `...@1`.
- A writer that committed meanwhile has already published `...@2` and advanced
  the pointer to `2`.
- The stale reader's write lands on `...@1` — **a key nobody will ever look up
  again.** Garbage, not corruption.
- Its attempt to move the pointer backwards is refused.

The refusal has to be atomic, which is why it is a Lua script and not
`GET`-then-`SET`:

```lua
local current = redis.call('GET', KEYS[1])
if current and tonumber(current) >= tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
```

`GET`-then-`SET` from the client would be the exact read-modify-write race we
are fixing, moved one layer down. Redis runs a script to completion with nothing
interleaved — that is what `EVAL` is *for*.

Where does `version` come from? A `BEFORE UPDATE` trigger on the table, not
application code. The moment the bump lives in a repository method, correctness
depends on every future writer remembering it: the backfill script, the second
service that got write access "just this once", the data fix someone runs in
psql at 2am. Push the invariant down to the layer that cannot be bypassed.

**What it costs, honestly:** one column, one extra `GET` on the read path, and
every superseded version lingering until its TTL. That last one is real —
version-keying trades correctness for keyspace, bounded by *write rate × TTL*.
Now the TTL is load-bearing for memory as well as for correctness.

## The full menu

| Approach | Closes the race | Cost | Use when |
|---|---|---|---|
| delete-after-commit + TTL | no | none | profile data, cosmetic staleness |
| delayed double delete | no, lower odds | a timer you can't size, 2× invalidations | you inherited it |
| **version-keyed entries** | **yes** | a column, a GET, keyspace | staleness is a *security* problem |
| write-through, never on read | yes, by construction | Redis write per DB write, cold after invalidation | small hot data: permission matrix, flags |
| **don't cache it** | yes, trivially | a database read | a 0.3ms indexed lookup, which is most of them |

That last row belongs on the list every time. A cache you added for tidiness is
a consistency bug you volunteered for.

## Run it

```bash
./lab.sh run 03
```

## The interview answer

> *"You delete the cache after the commit. Is that correct?"*

"It's correct for the common case and it isn't a proof. A reader that missed and
SELECTed just before my commit will SET after my delete, and my delete returns 0
so I never learn it did nothing. The cache is then wrong until the TTL. The
general reason is that the last writer to Redis wins, and cache-aside gives the
stale reader a chance to be last.

Delayed double delete lowers the odds with a timer nobody can size correctly.
What actually closes it is putting the row's version in the key, with the version
bumped by a trigger so no writer can skip it — a stale reader then writes to a
key that will never be read, and a Lua compare-and-set keeps the pointer from
moving backwards. I pay for that in keyspace and let the TTL collect it. For a
profile I wouldn't bother; for a permission check I would."
