# 04 — The customer gets charged twice

```bash
./lab.sh scenario 04
```

The most important scenario here. A payment service charges a card, then dies
before committing the offset. Kafka does the correct thing and hands the record
to the next consumer that asks. The correct thing charges the card again.

## What to notice

**Part A, no guard.** One `$40` order, two movements in the ledger, `$80` taken:

```
payment#1  <- orders/0@0000   OrderPlaced   o-1 widget x2 $40.00
payment#1                     o-1 charged $40.00
payment#1  !                  CRASH: work done for orders/0@0, offset NOT committed
...restart...
payment#1  <- orders/0@0000   OrderPlaced   o-1 widget x2 $40.00
payment#1  !                  DOUBLE CHARGE on o-1: customer has now paid $80.00
```

Look at the offsets. `orders/0@0000` twice. Kafka is not broken — it is doing
exactly what it promised, which is never to lose a record it has not been told
was handled.

**Part B, guard on.** Same crash, same redelivery, one charge. The guard
remembers the event ID and makes the second application a no-op.

**What did not change: the redelivery.** You cannot switch that off. The only
thing you control is whether applying a record twice is harmful.

## Why this is the default

The commit sits after the work in `internal/kafkaio/consumer.go`, and that one
line is the whole decision:

| Order | Semantics | Failure mode |
|---|---|---|
| work, then commit | at-least-once | duplicates |
| commit, then work | at-most-once | lost records |

There is no third option that a distributed system gives you for free. Since
you cannot have neither, take duplicates — a duplicate you can defend against
with an idempotency key, and money that vanished you cannot get back.

## "But Kafka has exactly-once"

It has exactly-once *within Kafka*: a transaction can atomically publish
records and commit consumer offsets, so a read-process-write pipeline that
stays inside Kafka can be made effectively once. Genuinely useful, and it is
how Kafka Streams works.

It does nothing for the card processor. The moment your side effect leaves
Kafka — an HTTP call, a row in Postgres, an email — you are back to
at-least-once plus idempotency, because no protocol can make a remote system's
side effect and your offset commit atomic. "Exactly-once delivery" across a
network is not a thing you can buy; "exactly-once *effect*" is a thing you
build.

## The subtle part: when to record the key

Look at the ordering in `internal/dedupe/dedupe.go` — the ID is written and
fsynced *before* the caller is told the event is new, and therefore before the
charge. That is a choice with a real cost:

- **Record first, then act** — a crash in between means the event is marked
  applied but never was. You lose an effect.
- **Act first, then record** — a crash in between means the effect happened and
  nothing remembers. You duplicate an effect.

Neither ordering is safe, because two separate systems cannot be updated
atomically by hoping. The way out is to stop having two steps: write the
idempotency key and the side effect in **one transaction** in **one database**,
so either both are durable or neither is. When the effect belongs to somebody
else's system, push the key to them — that is precisely what Stripe's
`Idempotency-Key` header is for.

This lab keeps the two-step version and fsyncs, so you can see the seam. Real
money would not.

## Why the ledger is a file

It would be shorter to keep the ledger in a `map` — and the demo would silently
stop working. The record is redelivered *because* the process died, and the
process dying is what emptied the map. The second charge would look like the
first. An in-memory idempotency table protects you against everything except
the thing that actually happens.

## Try it yourself

```bash
./lab.sh reset
./bin/payment -crash-after 3          # crash mid-batch instead
./bin/payment -crash-after 1 -idempotent
```

## Question to sit with

Your consumer writes a row to Postgres for every event. Someone suggests
"just make the consumer commit the offset first, then write the row — that way
we never double-write." What have they actually bought, and what have they
paid?

<details>
<summary>One answer</summary>

They have bought at-most-once: no duplicate rows, ever. They have paid with
silent data loss — any crash between the commit and the write drops that event
permanently, and nothing anywhere will ever tell you, because from Kafka's
point of view the record was handled.

Duplicates announce themselves. Missing rows do not. That asymmetry is why
at-least-once is the default almost everywhere, and why the answer to
"we might double-write" is a unique constraint on an idempotency key rather
than a reordering of the commit.
</details>
