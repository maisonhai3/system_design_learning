# Temporal Saga Lab — Go, four microservices, and the same order done two ways

An order-fulfilment saga across four Go services, orchestrated **twice over the
same services**: once by a plain in-process orchestrator, once by a Temporal
workflow. Then things are killed mid-flight and both are made to explain
themselves.

This repo already has `learn_microservice/`, which is about **choreography** —
services reacting to each other's events, with nobody in charge. This lab is the
other half: **orchestration**, with one thing in charge and a very specific
question — *what does it cost to make "in charge" survive the process being
killed?*

Every scenario is an **automated proof**: it forces the situation, asserts the
anomaly actually reproduced, asserts the fix actually held, and exits non-zero if
either stops being true.

## Quick start

```bash
./lab.sh up          # six containers: temporal + 4 services + a worker
./lab.sh run 01      # the one the lab exists for
./lab.sh run-all     # every proof; non-zero if any claim stops holding
open http://localhost:8233   # the Temporal Web UI — this is most of the value
```

No Docker? The whole lab runs as host processes against the Temporal CLI's dev
server:

```bash
brew install temporal   # or https://docs.temporal.io/cli
./lab.sh local-up
./lab.sh run-all
```

And a useful subset needs no infrastructure whatsoever:

```bash
./lab.sh test        # ~1s. Includes a 72-hour timeout and three replay checks.
```

## The scenarios

| # | Scenario | The claim you'll be able to defend |
|---|---|---|
| 01 | **The crash that does not matter** | Kill the process mid-saga: the naive order is gone with the money taken and **nobody is told**; the Temporal order resumes on another worker and is **not re-charged**. |
| 02 | **At-least-once is the deal** | Measured: the same fault, the same retry → **2 charges with a per-attempt key, 1 with a stable one** — and the double-charged order reports `COMPLETED`. |
| 03 | **Compensation is not rollback** | A saga cannot un-charge, only offset: **1 charge + 1 refund, net 0**, in reverse order, on a context that survives cancellation. And what happens when the rollback itself fails. |
| 04 | **The wait that outlives the process** | A 72-hour approval gate survives `kill -9` on every worker. The naive version's approval gets a **404**. |
| 05 | **Workflow code is replayed** | Adding a step to a workflow breaks **every order already in flight** with `TMPRL1100` — and `GetVersion` fixes it at the price of code you may not delete. |

Every number above is re-measured on your machine by `./lab.sh run-all`.

## What's running

```
                    :8110  orders ─────────┐  the front door
                             │             │  mode=temporal -> start a workflow
                             │             │  mode=naive    -> run a goroutine
                             │             │
                start workflow / signal / query
                             │             │
                    :7233  temporal ◀──────┘        :8233  Web UI
                             ▲
                        poll │ task queue "orders"
                             │
                          worker ──────┬──────────┬──────────┐
                     (stateless,       │          │          │
                      scale to 0..n)   ▼          ▼          ▼
                                  :8111       :8112       :8113
                                 payments   inventory    shipping
                                    │           │           │
                                 own db      own db      own db
```

Six containers, one command.

| port | what | why it's there |
|---|---|---|
| 8233 | **Temporal Web UI** | the event history of an order, which is the best explanation of durable execution there is |
| 8110 | orders | starts, queries and signals orders — in either mode |
| 8111 | payments | the service that can charge you twice |
| 8112 | inventory | the service that is allowed to say *no* |
| 8113 | shipping | the step that fails last, which is what makes it useful |
| 7233 | temporal gRPC | what workers and clients actually talk to |

**Three services, three separate SQLite files, and no way to join across them.**
That is the constraint the whole lab rests on: if these shared a database the
right answer would be one transaction, and every saga here would be elaborate
nonsense. Every saga is a confession that a transaction was not available.

## Read these two files side by side

| | |
|---|---|
| `internal/saga/workflow.go` | the process as a Temporal workflow |
| `internal/naive/naive.go` | the same process as a goroutine and a map |

They are about the same length. The naive one is **not a strawman**: it retries
with backoff, distinguishes business rejections from faults, compensates in
reverse order, and supports the human approval gate. Someone competent wrote it.

It has exactly one thing wrong with it, and scenario 01 is about what that costs.

## Play with it

```bash
./lab.sh order 1999                    # a normal order
./lab.sh order 99900                   # over $500 -> waits for a human
./lab.sh approve <id> true
./lab.sh order 1999 naive              # the same order, the other orchestrator
./lab.sh order-status <id>
./lab.sh ledger <id>                   # what the customer was ACTUALLY charged

# break things on purpose
./lab.sh chaos payments after_commit 1          # commit, then fail the response
./lab.sh chaos shipping before_commit -1        # carrier down until further notice
./lab.sh chaos payments before_commit -1 refund # refunds fail, charges don't
./lab.sh chaos payments off

./lab.sh status                        # services, stock, armed faults
./lab.sh ps | ./lab.sh logs worker
./lab.sh reset                         # clear the three service databases
```

The chaos modes are named after **where the failure lands relative to the
write**, because that is the only thing the caller cannot see and the only thing
that decides whether a retry is safe:

| mode | meaning |
|---|---|
| `before_commit` | the write did not happen. A retry is free. |
| `after_commit` | the write **did** happen and the caller was told it failed. A retry duplicates it — unless the caller brought a key. |
| `slow` | the write happens eventually. Whether that counts as a failure is decided by someone else's timeout. |

`after_commit` is not exotic. It is what every crashed process, dropped
connection and overloaded load balancer looks like from the outside. A system
that only survives `before_commit` has not been tested.

## What the Web UI is for

Open <http://localhost:8233>, pick a workflow, and read the event history top to
bottom. That list — `ActivityTaskScheduled`, `ActivityTaskCompleted`,
`TimerStarted`, `WorkflowExecutionSignaled` — **is** the order. It is what a
crashed workflow is rebuilt from, and it is why scenario 05's constraint exists.

If one thing from this lab is worth twenty minutes, it is reading one of those
histories next to `internal/saga/workflow.go` and matching them up line by line.

## The engineering argument, in one page

Temporal is not free and it is worth being precise about the bill.

**What you get.** Process-independent execution state. Retries, timeouts,
backoff and timers as declared data rather than hand-rolled loops. A durable
place for signals and queries. A complete, queryable audit log of every business
process, for free and without anyone remembering to write it.

**What you pay.**

- *A server to operate.* Real Temporal is a cluster plus Postgres or Cassandra,
  and it becomes a tier-0 dependency of every process you put on it. (Temporal
  Cloud moves the operational burden, not the dependency.)
- *Determinism, for ever.* Workflow code is a wire format whose reader is your
  own code from six months ago. Scenario 05 is the whole story, and `GetVersion`
  branches are deferred maintenance with a due date.
- *A second mental model.* Every engineer on the team now needs to know why they
  cannot call `time.Now()` in one file and can in the one next to it.
- *Exactly-once is still yours.* Scenario 02: the platform gives you
  at-least-once and a stable place to hang a key. It does not give you the key.

**When it is worth it.** Multi-step processes that cross service boundaries,
where a partial failure has a cost someone can name in currency — payments,
provisioning, fulfilment, onboarding — and where "wait for a human" or "wait for
a week" is part of the process.

**When it is not.** Anything a single database transaction covers. Request/reply
that either succeeds or is retried by the caller with no side effects. A pipeline
where "run the whole thing again tomorrow" is an acceptable recovery. If the
honest failure plan is *re-run the batch*, a saga is a liability and a cron job
is an architecture.

The interesting question this lab is trying to leave you with is not "should I
use Temporal". It is: **for each piece of state in my system, what process is
holding it, and what happens to it when that process is killed?** Once you ask
that out loud, most of the answers are uncomfortable — and Temporal is one of
several reasonable responses, alongside a durable state machine with a scheduler,
an outbox with a resumer, or deciding the loss is acceptable and saying so.

## Left out on purpose

Worth knowing they exist; they would have doubled the lab without doubling what
it teaches.

- **Heartbeats and long activities.** `CreateShipment` records one heartbeat and
  the options set a `HeartbeatTimeout`, but nothing here runs long enough to
  need it. Without heartbeats, a worker that dies mid-activity is not noticed
  until `StartToCloseTimeout` expires — however long that is.
- **Child workflows, continue-as-new, cron schedules.** The answer to "this
  workflow has run for a year and its history is enormous".
- **Task queue routing, worker versioning, sticky execution.** How you actually
  deploy workflow changes without scenario 05 happening to you.
- **Search attributes and the visibility store.** How you find workflows by
  business data — and why it is not `SELECT * FROM orders WHERE ...`.
- **A real persistence layer.** The dev server keeps history in memory; a real
  cluster's durability is its database's durability, and that database is the
  thing you back up.

## Layout

```
cmd/            five binaries: orders, payments, inventory, shipping, worker
internal/
  saga/         the workflow, the activities, the versioning variants, the tests
  naive/        the same saga as a goroutine and a map
  orders/       the front door: start / query / signal
  payments/     charges, refunds, and one UNIQUE index that does all the work
  inventory/    reservations, releases, and the 409 that must not be retried
  shipping/     the step that fails last
  chaos/        failure injection, named after where the failure lands
  store/        one SQLite file per service, and no way to join across them
  labhttp/      JSON, and the retryable/permanent distinction
  lab/          the scenario harness
scenarios/      five executable proofs, each with a README
testdata/       a real recorded history, replayed by the tests
```
