# 05 — Rebuilding state from nothing but the log

```bash
./lab.sh scenario 05
```

A second projector starts under a brand new consumer group, reads the whole
history from offset 0, and arrives at exactly the state the running one has.
Nobody kept a backup. The log *was* the backup.

## What to notice

**A new group name is the entire mechanism.** Kafka stores committed offsets per
group. A group that has never committed anything has nowhere to resume from, so
it starts at `StartOffset` — and ours is `FirstOffset`. That is all a replay is.

**`StartOffset` only applies to a group with no committed offset.** Once a group
has committed, Kafka resumes from there and the setting is ignored. This is
where hours go: *"I set FirstOffset, why didn't it replay?"* Because the group
remembered where it was. To genuinely start over you need a new group name, or
to reset the existing group's offsets.

**The original group never noticed.** Its committed offsets are untouched, its
lag stays zero. Reading is not consuming, so a new reader costs the existing
one nothing. This is the property that makes a log qualitatively different from
a queue: **adding a consumer is free**. A fraud checker, a revenue dashboard, a
replacement projector with a different schema — none of them require touching
the services that produce the events.

**The read model is a cache, not a source of truth.** If the projector is wrong,
you do not repair it. You throw it away and rebuild. That is a genuinely
different operational posture from "the database is the truth and we had better
not corrupt it".

## The part that took care

The scenario checks that the replayed state matches the live one, and the first
version of this lab **failed that check**. Live it said
`{cancelled: 2, confirmed: 1}`; replayed it said `{placed: 1, paid: 1,
confirmed: 1}`. Same log, same code, different answer.

The projector reads three topics. Kafka orders records within a partition and
promises nothing across partitions, let alone across topics. Live, events
arrive roughly as they happen, so a naive "last event wins" fold looks
perfectly correct. On a replay the consumer drains whatever is ready, so it can
apply `StockRejected` before the `PaymentCompleted` that preceded it in real
time — and the fold lands somewhere else.

The fix is not to chase cross-topic ordering, because Kafka does not sell it.
The fix is to make the fold not care. In `cmd/projector/main.go`, status only
ever moves forward:

```go
func advance(v view, status, reason string) view {
	if stage(status) <= stage(v.Status) {
		return v          // never go backwards
	}
	...
}
```

`Refunded` is a boolean that only goes false → true. `Updated` keeps the
maximum timestamp rather than the last one seen. All three are the same idea:

> A projection must be a function of the **set** of events, not of the order
> you happened to read them in.

Monotonic state, commutative merges — the same shape as a CRDT, and the same
reason. `TestApplyIsOrderIndependent` in `cmd/projector/apply_test.go` checks
every permutation, and it fails against the obvious implementation. That test
is the point; the bug is invisible until the day you rebuild a projection and
quietly get different numbers.

## Where this stops working

Replay is only free while the log still has the records. Kafka retains by time
or size (`retention.ms`, `retention.bytes`), and once a segment ages out it is
gone. If replaying from the beginning of history is a capability you actually
depend on, that is a retention setting you must choose on purpose — or a
compacted topic, which keeps the latest record per key forever and throws away
the rest.

Replay also re-runs your *consumers*, not your side effects — and a consumer
that sends email will send it all again. Which is why "just replay it" is safe
for a projector and dangerous for anything that touches the outside world.

## Try it yourself

```bash
./bin/projector -group "my-replay-$(date +%s)" -addr :8083
curl localhost:8083/stats
```

Then run it twice with the *same* group name and watch the second run read
nothing — it resumes where the first one stopped.

## Question to sit with

You need to add a field to the read model — say, how long each order took from
placed to confirmed. The data is on the log. What is the deployment plan, and
what is the risk?

<details>
<summary>One answer</summary>

Deploy the new projector under a new consumer group, let it rebuild from offset
0 alongside the old one, check it, then switch reads over and delete the old
group. No migration script, no backfill job, no downtime — the history is
already there.

The risks are worth naming. Rebuilding reads the entire topic, which is real
load on the brokers. It takes as long as it takes, and you cannot cut over
until it catches up. And it only works if the events contain what you need —
if nobody recorded a timestamp on `StockReserved`, the log cannot tell you what
it never held. That is the argument for events carrying a little more than
today's consumer strictly needs.
</details>
