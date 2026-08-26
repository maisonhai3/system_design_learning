# The avatar race — the scenario this lab exists for

**The claim you are practising:** *"Moving the avatar pointer out of `users` and
into `uploads.is_avatar` does not, on its own, fix anything. It trades a hot-row
update for a lost invariant, and you need a partial unique index to get the
invariant back."*

## The change under review

The review said:

> Move the avatar FK out of the users table entirely. Just query the Uploads
> table with `WHERE user_id = X AND is_avatar = true`. This eliminates the hot
> row update.

So the write becomes two statements:

```sql
UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar;
INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (1, 's3://new', true);
```

That is correct in a single-user demo and wrong under concurrency.

## What breaks

Two devices upload an avatar at the same moment. Session B's demote-`UPDATE`
blocks on A's row lock — good, that part works. But when A commits and B
unblocks, B **re-evaluates its `WHERE` clause at READ COMMITTED**, discovers
the old row is already `is_avatar = false`, and matches **zero rows**. B's
demote silently no-ops. Then B inserts its own `is_avatar = true` row.

Result: **two rows with `is_avatar = true` for one user.** No error was raised.
Both transactions committed. And now `WHERE user_id = 1 AND is_avatar = true`
returns two rows, so which avatar the user sees depends on which one the
planner happens to hand back first — the avatar appears to flicker between two
images across page loads and across replicas.

The nastiest part: `is_avatar` is a *convention*, not a constraint. Nothing in
the schema says "one avatar per user", so nothing stops the database from
storing a state your application considers impossible.

## The four options, and what each one actually costs

| Option | Invariant | Keeps history | Round trips | Write contention | How it fails |
|---|---|---|---|---|---|
| Two statements, no index | ✗ **broken** | ✓ | 2 | row lock | silently: two live avatars |
| Two statements **+ partial unique index** | ✓ | ✓ | 2 | row lock | loudly: `23505`, you retry |
| `INSERT … ON CONFLICT … DO UPDATE` | ✓ | ✗ **destroyed** | 1 | row lock | doesn't |
| Newest row wins (no flag at all) | ✓ by construction | ✓ | 1 | **none** | clock skew; no manual revert |
| FK on `users.avatar_upload_id` | ✓ | ✓ | 2 | **hot row** | see scenario 07 |

There is no free option. Picking one is the interview answer; pretending one is
free is the interview failure.

## Three things in here that will surprise you

**1. `ON CONFLICT … DO UPDATE` does not insert a row.** It updates the existing
one in place. So this:

```sql
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://new', true)
ON CONFLICT (user_id) WHERE is_avatar DO UPDATE
  SET storage_url = EXCLUDED.storage_url;
```

leaves you with exactly **one** row for that user, forever. Every previous
avatar is overwritten. If the reason you moved avatars into `uploads` was to
keep upload history, this fix quietly deletes the thing you were trying to
keep. The runner proves it by counting rows before and after.

**2. You must repeat the index predicate in the conflict target.** `ON CONFLICT
(user_id)` alone is rejected with *"there is no unique or exclusion constraint
matching the ON CONFLICT specification"* — the planner will not infer a partial
index unless you restate its `WHERE`.

**3. Wrapping the demote and the promote in a CTE does not make them atomic —
it makes them fail every single time.** This looks like the clever fix:

```sql
WITH demoted AS (
    UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar RETURNING id
)
INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (1, 's3://new', true);
```

It raises `23505` **100% of the time, with no concurrency at all**. All parts of
a statement — including data-modifying CTEs — share one snapshot, and the
`INSERT`'s uniqueness check still sees the index entry the CTE is in the middle
of removing. *Atomic* does not mean *sequential*. Two ordinary statements in one
transaction work fine precisely because the second one can see the first.

## Run it

    ./lab.sh run 03          # automated proof of all of the above
    ./lab.sh a 03            # terminal 1
    ./lab.sh b 03            # terminal 2

## The interview version

> "The review was half right. Taking the avatar pointer off `users` did remove
> a hot row, but `is_avatar` is a convention, not a constraint — so two
> concurrent uploads each demote the old row, each insert a new one, and you end
> up with two live avatars and no error. The fix isn't the column layout, it's
> `CREATE UNIQUE INDEX … ON uploads (user_id) WHERE is_avatar` — that turns
> silent corruption into a `23505` we retry. We looked at `ON CONFLICT` as a
> single-round-trip version, but it updates the existing row in place, which
> would have destroyed the upload history that was the reason for the table in
> the first place. What we actually shipped was append-only with newest-row-wins,
> because it has no write contention at all — the tradeoff being that reverting
> to an old avatar needs its own endpoint."
