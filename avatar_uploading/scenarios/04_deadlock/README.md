# Deadlock — and why the fix is an ordering convention, not a lock

**The claim you are practising:** *"Deadlocks are not a database failure, they
are two transactions taking the same locks in different orders. Postgres
detects them in `deadlock_timeout` and kills one; the fix is to always acquire
locks in a deterministic order."*

## What breaks

A transfers money to B while B transfers money to A:

```
A: UPDATE accounts ... WHERE owner = 'alice'   -- locks alice
B: UPDATE accounts ... WHERE owner = 'bob'     -- locks bob
A: UPDATE accounts ... WHERE owner = 'bob'     -- waits for B
B: UPDATE accounts ... WHERE owner = 'alice'   -- waits for A
```

Neither can proceed and neither will give up. After `deadlock_timeout` (1s by
default; this lab sets **500ms** so you are not sitting there waiting) Postgres
runs its detector, finds the cycle, and aborts one transaction with
**`40P01 deadlock_detected`**. The survivor commits normally.

## Three things worth knowing

**Postgres does not prevent deadlocks, it detects them.** The check only runs
after a lock wait has already exceeded `deadlock_timeout`, because scanning the
wait graph on every lock acquisition would be far too expensive. Lowering
`deadlock_timeout` makes detection faster and every ordinary lock wait more
expensive. Do not tune it as a fix.

**The victim is not random and not your choice.** Postgres aborts whichever
transaction's process detects the cycle. You cannot nominate a winner, so
*every* transaction in a deadlock-prone path needs retry logic — you cannot
assume you will be the survivor.

**`40P01` is retryable; `23505` is usually not.** A deadlock victim did nothing
wrong and will typically succeed on a second attempt. Bundle it with `40001`
(serialization failure, scenario 06) in the same retry-with-backoff handler.

## The fix

Sort the rows you are going to lock, by a stable key, before you touch any of
them:

```sql
BEGIN;
SELECT id FROM accounts WHERE owner IN ('alice','bob') ORDER BY id FOR UPDATE;
-- both transactions now hold the locks in the same order; the second one
-- simply waits for the first instead of forming a cycle
```

The convention costs one extra statement and removes the whole class of bug.
Anywhere your code locks a *set* of rows — batch updates, multi-row transfers,
bulk imports — sort first.

## Run it

    ./lab.sh run 04
    ./lab.sh a 04      # terminal 1
    ./lab.sh b 04      # terminal 2

While it hangs, `./lab.sh logs` shows the server's own deadlock report,
including both queries and the exact cycle it found.

## The interview version

> "We had deadlocks on a two-row transfer — two requests grabbing the same
> pair of rows in opposite orders. Postgres detects the cycle after
> `deadlock_timeout` and kills one side with `40P01`. We fixed it by always
> locking rows in primary-key order, which turns the cycle into an ordinary
> wait, and we kept a retry handler for `40P01` and `40001` anyway, because you
> never get to choose whether you are the victim."
