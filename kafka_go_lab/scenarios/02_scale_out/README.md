# 02 — Three consumers, one group, and a rebalance

```bash
./lab.sh scenario 02
```

Stop the payment service, throw 30 orders at the system, watch the backlog
build. Then start three payment services and watch them split the work.

## What to notice

**The gateway never slowed down.** 30 orders accepted in about 50ms with nobody
to charge them. The backlog moved to the log, where it is a number you can watch
instead of an outage. This is the most underrated property of putting a log
between services: a slow consumer becomes a metric, not a failure.

**That number is consumer lag.**

```
lag = log end offset − committed offset
```

The `lag` table shows it per partition, because that is where it actually
lives — a group can be perfectly caught up on two partitions and hours behind
on a third, which is exactly what a hot key looks like.

**Three consumers in one group take one partition each.** Same `GroupID`, so
Kafka splits the partitions between them. A fourth would have sat idle:
**partitions cap how far you can scale out**. That is the number you cannot
easily change later — adding partitions rehashes keys onto different
partitions, so a key's history stays on the old one while its future goes to a
new one, and the per-key ordering you bought partitions for quietly breaks.
Pick a count comfortably above the consumers you expect to need.

**Killing one triggers a rebalance.** Its partition gets reassigned within
seconds and nothing is lost, because the work-to-do lives on the log rather
than in the dead process's memory. The final summary shows what each survivor
consumed *since* the kill: two consumers, three partitions, so one of them now
holds two.

Note that a partition is never served by two members of the same group at once
— that is what makes "one consumer per partition" a guarantee you can build on
rather than a tendency.

**Rebalances are not free.** While one happens, *every* consumer in the group
stops. A group that rebalances constantly can spend more time rebalancing than
working, which is why the timeouts in `internal/kafkaio/consumer.go` are a real
trade and not just tuning:

```go
HeartbeatInterval: 1 * time.Second,
SessionTimeout:    6 * time.Second,
RebalanceTimeout:  6 * time.Second,
```

Kafka cannot tell a dead consumer from a slow one — all it has is heartbeats.
Long timeouts mean a crashed consumer's partitions sit unserved for that long,
which is why restarting a consumer feels like it hangs. Short timeouts reclaim
work quickly but evict consumers that were merely busy, triggering a rebalance
that stops everyone and makes the backlog worse. The defaults are 30 seconds;
these are turned down so the lab moves at human speed.

## Try it yourself

```bash
./lab.sh flood 500        # in one terminal
./lab.sh lag              # in another, repeatedly — watch it climb, then drain
```

Then try running **four** payment instances against three partitions and check
the summary. One of them will have consumed nothing.

## Question to sit with

Your consumers are keeping up on average, but lag spikes to 50,000 every
weekday at 09:00 and drains by 09:20. Adding consumers does nothing. Why might
that be — and what would you look at first?

<details>
<summary>One answer</summary>

If adding consumers does nothing, you are almost certainly partition-bound
rather than consumer-bound: either you already have one consumer per partition,
or the lag is concentrated on a few partitions. Look at lag *per partition*
first. Even spread means you need more partitions (and more consumers). A few
hot partitions means your key is skewed — one enormous customer, or a key like
"country" where 80% of traffic is one value. More consumers cannot help,
because one partition is only ever served by one member of the group. The fix
is a better key, not more machines.
</details>
