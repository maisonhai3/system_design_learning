# Read anomalies — what each isolation level actually buys you

**The claim you are practising:** *"Postgres has no dirty reads at any setting,
READ COMMITTED gives you a fresh snapshot per statement, and REPEATABLE READ
gives you one snapshot for the whole transaction — which kills non-repeatable
reads AND phantoms, even though the SQL standard only asks it to kill the first."*

## The three anomalies

| Anomaly | You read a row… | Postgres READ COMMITTED | Postgres REPEATABLE READ |
|---|---|---|---|
| Dirty read | …someone else has not committed | **impossible** | impossible |
| Non-repeatable read | …twice, and it changed | happens | prevented |
| Phantom read | …by predicate twice, and the *set* changed | happens | prevented |

Two things on that table are worth saying out loud in an interview.

**Postgres cannot do dirty reads at all.** `SET TRANSACTION ISOLATION LEVEL
READ UNCOMMITTED` is accepted without complaint, and `SHOW
transaction_isolation` will even echo `read uncommitted` back at you — but the
behaviour is plain READ COMMITTED. MVCC has no mechanism for exposing an
uncommitted row version to another transaction. So the setting is a no-op that
*reports success*, and the only way to discover that is to test the behaviour.
If someone asks you to "drop to read uncommitted for speed", there is nothing
to drop to.

**Postgres REPEATABLE READ also prevents phantoms.** The standard permits
phantoms at that level; Postgres implements it as true snapshot isolation, so
your whole transaction sees one frozen instant of the database. This is
stronger than required — do not assume the same code is safe on MySQL or on
Oracle, where REPEATABLE READ means something different.

## The one it does *not* fix

Snapshot isolation still permits **write skew**: two transactions read an
overlapping set, then write *disjoint* rows, and each one's decision is
invalidated by the other. No row is written twice, so there is no conflict to
detect. That needs SERIALIZABLE — scenario 06.

## The cost of REPEATABLE READ

A frozen snapshot is not free. The rows your transaction might still need
cannot be vacuumed while it is open, so a long-lived REPEATABLE READ
transaction makes dead tuples pile up across the whole database. That is how a
forgotten reporting query turns into table bloat and a slow production
database. Keep them short.

## Run it

    ./lab.sh run 02
    ./lab.sh a 02      # terminal 1
    ./lab.sh b 02      # terminal 2

## The interview version

> "Postgres defaults to READ COMMITTED, so each *statement* gets a fresh
> snapshot — you never see uncommitted data, but two reads in one transaction
> can disagree. When a request needed a consistent view across several queries
> we opened it REPEATABLE READ, which in Postgres is real snapshot isolation
> and kills phantoms too, not just non-repeatable reads. The thing we watched
> for was leaving those transactions open — a frozen snapshot blocks vacuum and
> bloats the tables."
