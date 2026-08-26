# Write skew and SERIALIZABLE — the anomaly snapshot isolation cannot see

**The claim you are practising:** *"Write skew is the anomaly REPEATABLE READ
does not catch, because the two transactions write different rows. Only
SERIALIZABLE catches it, and the price of SERIALIZABLE is that you must write a
retry loop."*

## What breaks

The rule is "the accounts must hold at least 100,000 between them". Both
accounts hold 100,000, so the total is 200,000 and a single 100,000 withdrawal
is clearly fine.

```
A: SELECT sum(balance_cents) FROM accounts     -- 200000, plenty
B: SELECT sum(balance_cents) FROM accounts     -- 200000, plenty
A: UPDATE accounts SET balance = balance - 100000 WHERE owner = 'alice'
B: UPDATE accounts SET balance = balance - 100000 WHERE owner = 'bob'
A: COMMIT      -- fine
B: COMMIT      -- fine
```

Total: **zero**. Each transaction made a decision that was correct when it read,
and wrong by the time it committed. Both committed successfully.

## Why REPEATABLE READ does not save you

This is the part worth memorising, because it is the most common wrong answer:

> **Postgres REPEATABLE READ does not prevent write skew.**

The two transactions **read** an overlapping set but **write disjoint rows** —
A writes alice, B writes bob. There is no row that both wrote, so there is no
first-updater-wins conflict for snapshot isolation to detect. Scenario 02 showed
REPEATABLE READ killing non-repeatable reads and phantoms; this is the thing it
lets through. The runner proves it by running the same interleaving at all
three levels.

| Isolation level | Outcome |
|---|---|
| READ COMMITTED | both commit, total 0 — **invariant violated** |
| REPEATABLE READ | both commit, total 0 — **invariant still violated** |
| SERIALIZABLE | second commit fails `40001`, total 100000 — invariant holds |

## How SERIALIZABLE catches it

Serializable Snapshot Isolation tracks the read/write dependencies between
concurrent transactions and looks for a *dangerous structure* — a pattern of
read-write edges that no serial ordering could produce. When it finds one it
aborts a transaction with `40001 serialization_failure`.

Note what that means: **the failure can arrive at `COMMIT`, not at a
statement.** Your transaction can run to completion, look entirely successful,
and then be rejected on the way out. Code that assumes "if the statements
worked, the commit works" breaks here.

Where exactly it lands depends on the interleaving, because SSI aborts as soon
as it can *prove* the dangerous structure exists:

| Interleaving | Where `40001` fires |
|---|---|
| Both write, then both commit | at B's **`COMMIT`** — the proof only exists then |
| A commits, then B writes | at B's **`UPDATE`** — the proof already exists |

You cannot rely on either, which is why the retry has to wrap the whole
transaction rather than just the commit. The runner demonstrates both.

## The price

**You must retry.** `40001` is not a bug report, it is the database telling you
to run the transaction again — and it means any transaction under SERIALIZABLE
must be safe to run twice. Side effects that escape the database (charging a
card, sending an email, publishing to a queue) must happen *after* the commit
succeeds, or be made idempotent.

**It costs throughput under contention.** SSI keeps predicate lock information
for every in-flight transaction, and the more they overlap the more often it
aborts them. It is the right tool for a small number of genuinely
invariant-critical paths, not a global default.

**A cheaper alternative usually exists.** If you can express the invariant as a
constraint (`CHECK`, a unique index — see scenario 03) or materialise the
conflict onto a single row that both transactions must lock (`SELECT … FOR
UPDATE` on a parent row), you get the same safety without the retry loop.

## Run it

    ./lab.sh run 06
    ./lab.sh a 06      # terminal 1
    ./lab.sh b 06      # terminal 2

## The interview version

> "The bug was write skew — two requests each read the same totals, each made a
> decision that was valid at read time, and each wrote a *different* row, so
> there was no write conflict for snapshot isolation to catch. REPEATABLE READ
> doesn't help; that's the part people get wrong. We put that one path under
> SERIALIZABLE with a retry-on-`40001` wrapper, and made sure the transaction
> had no side effects outside the database so retrying was safe. Everything else
> stayed on READ COMMITTED — SSI costs too much to use as a default."
