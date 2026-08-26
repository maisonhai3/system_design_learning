# Avatar Uploading — a SQL concurrency lab

A throwaway Postgres you can break on purpose, built around one real design
question: **where should "this user's current avatar" actually live?**

That question turns out to be a tour of every concurrency problem worth knowing
— lost updates, isolation levels, unique constraints under race, deadlocks,
queue locking, write skew, and hot-row contention — and it ends at the one that
takes APIs down at scale: connection exhaustion.

Every scenario comes in two forms. **Paste the SQL into two terminals** and
watch a transaction block in real time, or **run the automated proof** that
forces the interleaving and asserts the anomaly actually happened.

## Quick start

```bash
./lab.sh up          # postgres on localhost:5433, schema + seed applied
./lab.sh list        # what's in here
./lab.sh read 03     # the scenario this lab exists for
./lab.sh run 03      # watch it break, then watch four fixes
```

Two terminals, to feel it rather than read it:

```bash
./lab.sh a 03        # terminal 1 — prints the steps, then drops you into psql
```

```bash
./lab.sh b 03        # terminal 2 — run each step when terminal 1 says to
```

A third terminal, to see who is stuck behind whom:

```bash
./lab.sh watch
```

## The scenarios

| # | Scenario | The claim you'll be able to defend |
|---|---|---|
| 01 | Lost update | Read-modify-write across two round trips is a bug at any level below SERIALIZABLE. Three fixes, in the order to reach for them. |
| 02 | Read anomalies | Postgres has no dirty reads at all; REPEATABLE READ kills phantoms too, which the standard doesn't require. |
| 03 | **The avatar race** | Moving the pointer off `users` loses the invariant. `is_avatar` is a convention until a partial unique index makes it a constraint. |
| 04 | Deadlock | Not a database failure — two transactions taking the same locks in different orders. The fix is sorting, not locking. |
| 05 | `SKIP LOCKED` | `FOR UPDATE` gives you a correct queue that is also single-threaded. Measured: **4.7x** faster with `SKIP LOCKED`. |
| 06 | Write skew | The anomaly REPEATABLE READ does *not* catch, because the writes are disjoint. Only SERIALIZABLE — and you must retry. |
| 07 | **The hot row** | Per-row write throughput is `1 / transaction_duration`. Measured: **27x** at 40 clients, and pgbench shows it flatlining. |

Run every proof at once — non-zero exit if any claim stops holding:

```bash
./lab.sh run-all
```

## The two interview questions this is built for

**"You said move the avatar FK out of `users` — walk me through that."**
Scenario 03. The honest answer is that the review was half right: it removes a
hot row and silently drops the invariant. There are four ways to get the
invariant back and none of them are free — one of them (`ON CONFLICT`) quietly
destroys the upload history that was the whole reason for the table. The
scenario proves each claim rather than asserting it.

**"APIs supporting 10,000 CCU — how did you load-test it, and where did it
break first?"** Scenario 07 plus `bench/`. You get a real measured number from
pgbench on your own machine:

```bash
./lab.sh bench       # hot row vs spread rows vs append-only, at -c 1/10/25/40
./lab.sh exhaust     # 100 clients at a 50-connection server, and the fix
```

The shape to expect — and the reason it is a good answer — is that the hot-row
column **stops improving entirely** while the others keep scaling:

```
  clients          hot row    spread rows    append-only hot vs spread
  --------------------------------------------------------------------
  1                    306            316            343         1.0x
  10                   346           2484           2497         7.2x
  40                   347           9481           9446        27.3x
```

Same server, same statement, same work per transaction. The only difference is
whether the writers converge on one row. Then `./lab.sh exhaust` closes the
loop: every request blocked on that row is still holding a connection, so a
single contended row exhausts the pool and takes down endpoints that have
nothing to do with avatars. That is what "the database fell over" usually means.

## Layout

```
lab.sh                one entry point for everything
docker-compose.yml    postgres:16 on :5433, deliberately mis-tuned for teaching
schema/               01_schema.sql, 02_seed.sql — idempotent, re-run on reset
scenarios/NN_name/
    README.md         what breaks, why, the fixes, and the interview answer
    a.sql, b.sql      numbered steps for two psql terminals
    run.py            the automated proof (uv runs it; no venv needed)
lab/harness.py        shared session/trace/assertion plumbing
bench/                pgbench scripts + the connection-exhaustion demo
observe/              the pg_stat_activity queries worth memorising
```

## How the proofs work

Each `run.py` asserts **two** kinds of claim, and the run fails if either stops
holding:

- `ANOMALY REPRODUCED` — the naive version really did break
- `FIX HELD` — the fix really did work

The first one matters more than it looks. A teaching lab that quietly stops
demonstrating its own bug — because a Postgres version changed, or a fix was
too eager — is worse than no lab, because you would walk into the interview
still believing it. `./lab.sh run-all` exits non-zero the moment that happens.

Output is a trace with parameters rendered inline, so it reads like the SQL you
would have typed:

```
  A │ BEGIN
  A │ UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar  → 1 row(s) affected
  B │ UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar  … issued, waiting
  B │ UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar  ⧗ still blocked after 1s
```

## Notes

**Port 5433, not 5432.** This machine already runs a native PostgreSQL cluster
on 5432; the lab never touches it.

**The server is mis-tuned on purpose.** `max_connections = 50` so a laptop can
exhaust it, `deadlock_timeout = 500ms` so scenario 04 fails fast, and
`log_lock_waits = on` so `./lab.sh logs` shows you every wait over 500ms and
every deadlock cycle. Do not copy these settings anywhere real.

**Reset freely.** `./lab.sh reset` re-applies schema and seed in about a second;
every `run.py` calls it first, so scenarios never contaminate each other.
`./lab.sh down` deletes the database entirely.

**Requirements:** Docker, `psql` and `pgbench` (`postgresql-client`), and
[`uv`](https://docs.astral.sh/uv/) for the runners — which fetch `psycopg`
themselves via inline script metadata, so there is no virtualenv to manage.

**pgbouncer is optional and unverified.** `./lab.sh pool up` starts a real
pgbouncer on :6433, but the image was never pulled here (no registry access at
build time), so that path is the one thing in this lab that has not been run.
`./lab.sh exhaust` does not need it — it demonstrates the same lesson with a
client-side pool, which is verified.
