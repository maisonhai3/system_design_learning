# Kafka, four Go services, and a log you can watch

A toy order pipeline. Four small Go binaries, one Kafka broker, three topics.
Every line of output tells you which partition and which offset it came from,
because that is the difference between "Kafka is a queue, I suppose" and being
able to see what is actually happening.

```
  POST /orders
       │
       ▼
  ┌─────────┐   OrderPlaced    ╔═══════════╗
  │ gateway │ ───────────────▶ ║  orders   ║
  └─────────┘                  ╚═════╤═════╝
   202 Accepted                      │
                                     ▼
                               ┌─────────┐
                    ┌────────▶ │ payment │ charges, declines, and later
                    │          └────┬────┘ refunds itself
                    │               │ PaymentCompleted / PaymentFailed
                    │               ▼      / PaymentRefunded
                    │         ╔═══════════╗
                    │         ║ payments  ║
                    │         ╚═════╤═════╝
                    │               ▼
                    │         ┌───────────┐
                    │         │ inventory │ reserves stock, or rejects it
                    │         └─────┬─────┘
                    │               │ StockReserved / StockRejected
                    │               ▼
                    │         ╔═══════════╗
                    └───────── ║   stock   ║
                  StockRejected ╚═══════════╝
                                     
  projector reads all three topics and answers GET /orders/{id} on :8081
```

No service knows another service exists. They know topic names. Delete the
inventory service and payment keeps running — that is the actual test of
coupling, not whether things live in separate repositories.

## Run it

```bash
./lab.sh up      # Kafka in Docker, topics created
./lab.sh run     # the four services, interleaved in this terminal
```

Then, in another terminal:

```bash
./lab.sh demo    # three orders, three different endings
./lab.sh status  # what the read model thinks happened
```

Requires Go 1.23+ and Docker. If you already have a broker somewhere,
`KAFKA_BROKERS=host:9092 ./lab.sh run` and skip Docker entirely.

`go test ./...` runs without a broker at all — the domain logic is deliberately
separable from the plumbing.

## The three orders

| Order | What happens | What it shows |
|---|---|---|
| `widget x2` ($40) | paid, reserved, **confirmed** | the happy path end to end |
| `gizmo x4` ($600) | over the payment limit, **cancelled** | a service declining on its own authority |
| `doohickey x1` ($300) | paid, then out of stock, refunded, **cancelled** | a saga with no coordinator |

The third one is the interesting one. Inventory has no idea a refund exists; it
publishes "I rejected that stock" as a plain fact. Payment happens to be
listening and refunds itself. Nobody orchestrates it, so there is no
coordinator to be a single point of failure — and equally no single place to
look to find out why an order ended up cancelled. That is the trade
choreography makes, and it is a real one.

## The three ideas worth taking away

**A topic is an append-only file, not a queue.** Reading does not consume.
`./lab.sh dump orders` prints the records again, and again, as often as you
like. Everything else Kafka does is bookkeeping on top of "append here, read
from position N".

**A partition is the unit of both ordering and parallelism.** "Kafka guarantees
ordering" is false. "Kafka guarantees ordering *within a partition*" is true,
and the message key picks the partition: `partition = hash(key) % count`. We
key by order ID, so everything about order `o-7` is in sequence while `o-7` and
`o-8` proceed in parallel. Ordering costs you concurrency, so buy exactly as
much as the domain needs — and no more.

**A consumer group is a cursor into the log.** Members of the *same* group split
the partitions between them, which is how you scale out. *Different* groups
each get every record, which is how payment, inventory and the projector all
read the same topics without knowing about each other. Change the group name
and you get a brand new reader of the whole history, which is all a "replay"
really is.

## You have already written the Python version of this

`learn_microservice/choreography_async/` in this repo is the same shape — a
broker, publishers, subscribers, a saga, an audit service watching everything.
It is worth comparing, because it is a good broker and the gap is exactly what
Kafka is for:

| | `choreography_async/broker.py` | Kafka |
|---|---|---|
| Where events live | a `dict` of subscribers | files on disk, kept for days |
| Subscriber joins late | missed everything | reads from offset 0 |
| Broker restarts | history gone | history is the point |
| Two copies of a service | both get every event | group members split the work |
| Consumer crashes mid-event | event lost | redelivered — see scenario 04 |
| Ordering | whatever the event loop did | per partition, guaranteed |
| Backpressure visibility | none | consumer lag, in records |

The Python broker is 30 lines and teaches choreography perfectly. What it
cannot teach is what happens on a bad day, which is most of what Kafka is.

## The scenarios

Each is a script plus a README. Run them in order; each resets the log first so
it tells the same story every time.

```bash
./lab.sh scenario 01
```

| | | |
|---|---|---|
| `01_happy_path` | one order end to end | topics, partitions, offsets |
| `02_scale_out` | three consumers, one group, then kill one | rebalancing, consumer lag |
| `03_ordering` | 40 orders and one with two chapters | keys, partitions, what is *not* ordered |
| `04_duplicates` | crash between the work and the commit | at-least-once, idempotency |
| `05_replay` | a new group rebuilds the read model from zero | the log as source of truth |

Scenario 04 is the one to read twice. It is the difference between knowing the
phrase "at-least-once delivery" and having watched a customer get charged
eighty dollars for a forty dollar order.

## Poking at it by hand

```bash
./lab.sh order widget 3 ada    # place one order
./lab.sh lag                   # how far behind each consumer group is
./lab.sh dump payments         # every record on a topic, oldest first
./lab.sh flood 200             # a burst, to watch lag appear and drain
./lab.sh stats                 # counts by status, records per partition
./lab.sh reset                 # empty topics, offsets and ledger
```

`./lab.sh lag` is worth understanding rather than just running:

```
lag = log end offset − committed offset
```

It is a count of records, not a duration. Ten thousand is nothing on a topic
doing 100k/s and an outage on one doing 10/s. Flat and non-zero means you are
keeping up but started behind. Climbing means your consumers are slower than
your producers, and restarting them will not help.

There is a web UI too — `docker compose --profile ui up -d`, then
<http://localhost:8090>. Worth a look once. Everything it shows comes from the
same two admin calls `labctl` makes, and knowing which ones they are is what
lets you debug a cluster that has no UI attached.

## Where the toy ends

Things this lab does the small way, so you know what to distrust:

- **One broker, replication factor 1.** Every interesting durability question —
  in-sync replicas, leader election, `min.insync.replicas` — needs three.
- **JSON with no schema registry.** Fine until a producer renames a field and
  you find out in production. Real systems use Avro or Protobuf with a registry
  that refuses incompatible changes.
- **No dead-letter topic.** An undecodable record is logged and skipped. Retry
  it forever instead and you block every record behind it on that partition —
  the classic poison-pill outage.
- **No transactions.** Kafka can write a record and commit an offset atomically,
  which gets you exactly-once *within Kafka*. It does not extend to the card
  processor, which is why scenario 04 solves the problem with an idempotency
  key instead.
- **State in a local file.** The payment ledger and the idempotency table are
  append-only files, because in-memory versions cannot survive the crash they
  exist to defend against. In production both are rows in a database, ideally
  written in the same transaction as the effect they guard.

## Map

```
cmd/gateway      HTTP in, facts out. Owns the price list.
cmd/payment      Owns the money. Two consumer groups: one to charge, one to refund.
cmd/inventory    Owns the shelf.
cmd/projector    Owns nothing. A fold over the log, served as JSON.
cmd/labctl       topics / lag / dump / flood — the window into the broker.

internal/event    The contract. Data only, no behaviour.
internal/kafkaio  Producer and consumer, with the settings that matter explained.
internal/dedupe   An idempotency table that survives a restart.
internal/console  Aligned, colour-coded output. Observability starts here.
```

Read `internal/kafkaio/consumer.go` first. The fetch → work → commit loop is
twenty lines, and every delivery-semantics argument you will ever have is about
the order of those three.
