# 03 — What Kafka orders, and what it does not

```bash
./lab.sh scenario 03
```

40 orders, then one order whose story has two chapters.

## What to notice

**Keys spread, but each key stays put.** The scenario checks all 40 keys and
reports how many appear on more than one partition. The answer is always zero,
because `partition = hash(key) % 3` is a pure function of the key. That is the
entire ordering guarantee, and it is worth stating precisely:

> Kafka orders records **within a partition**. Nothing else.

**The spread is even, not equal.** 13/13/14, not 13.3 each. Hashing gives you
statistical balance, not exact balance — and with a skewed key (a customer who
is 40% of your traffic) it gives you no balance at all. A partition is only ever
served by one consumer in a group, so a hot key becomes a hot partition becomes
one overloaded consumer that you cannot scale your way out of.

**One key, two chapters, in order.** The doohickey order produces
`PaymentCompleted` and then, after a round trip through inventory,
`PaymentRefunded`. Both land on the same partition of `payments` at consecutive
offsets, in the order they happened. Keying by order ID is what makes "the
refund never appears before the charge" true.

**There is no global order.** Every partition has its own offset counter, all
starting at 0. Offset 5 on partition 0 and offset 5 on partition 2 say nothing
about which happened first. If you need to compare across partitions, you need
a timestamp or a sequence number that *you* put in the payload — Kafka will not
give you one, and the offset is not it.

## The design question underneath

Choosing a key is choosing what to order, and ordering costs concurrency:

| Key | What is ordered | What it costs |
|---|---|---|
| order ID | everything about one order | nothing much — orders are independent |
| customer ID | all of one customer's orders | a busy customer serialises onto one consumer |
| `null` (round-robin) | nothing | perfect balance, no guarantees at all |
| a constant | everything, globally | one partition does all the work |

Most people reach for the finest-grained key that still orders what the domain
needs. "Order everything" and "order nothing" are both available, and both are
usually wrong.

## Try it yourself

```bash
./lab.sh flood 200
./lab.sh stats     # records_by_partition — watch the balance
./lab.sh dump orders | head -40
```

## Question to sit with

You are keying by order ID. A product manager asks for a feature: a customer
can never have two orders being paid at the same time. What changes, and what
does it cost?

<details>
<summary>One answer</summary>

You would key by customer ID instead, so all of one customer's orders land on
one partition and are processed in sequence by one consumer. The cost is
immediate: your biggest customer's orders now queue behind each other, and you
cannot scale that customer's throughput by adding consumers, because a
partition is served by exactly one group member.

Before accepting that, check whether the requirement is really about ordering
or about mutual exclusion. If it is exclusion, a lock or a conditional update
in the payment service's own database gives it to you without serialising the
customer's whole event stream.
</details>
