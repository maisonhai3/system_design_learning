# At-least-once is the deal, and you pay the difference

**The claim you are practising:** *"Temporal guarantees an activity runs at
least once. Exactly-once is not on the menu and never was — what you get instead
is a stable place to hang an idempotency key, and the discipline to use it."*

## The fault that matters

Not "the payment service is down" — that one is easy, nothing happened and you
retry. The one that matters is:

```
worker ──POST /charges──▶ payments
                          INSERT INTO charges ✅   <- the money moved
                          ✗ 500 / timeout / RST    <- the caller never finds out
worker ◀──────────────────
       "that failed, I'll retry"
```

From outside, this is indistinguishable from a request that never landed. It is
what a crashed process, a dropped connection, a load balancer timeout and an OOM
kill all look like. The lab produces it on demand with
`chaos mode=after_commit`, which is the whole reason the chaos modes are named
after *where the failure lands relative to the write*.

## What the scenario measures

| idempotency key | charges | taken | order says |
|---|---|---|---|
| `per-attempt` — `uuid.New()` inside the activity | **2** | **2× the amount** | COMPLETED |
| `off` — no key | **2** | **2× the amount** | COMPLETED |
| `stable` — computed in workflow code | 1 | the amount | COMPLETED |

Look at the third column of the first row and then at the last. The customer was
charged twice and **the order reports success**. No alert fires. Nothing retries.
The first anyone hears about it is the chargeback.

## The line that does the work

In `internal/saga/workflow.go`:

```go
err = workflow.ExecuteActivity(ctx, a.ChargePayment, ChargeInput{
    OrderID:        in.OrderID,
    AmountCents:    in.AmountCents,
    IdempotencyKey: in.OrderID + "/charge",   // <- computed HERE
})
```

The key is built in *workflow* code, from data already in history. That is what
makes it stable across every retry of the activity and every replay after a
crash.

Now notice something about the broken variant: **the workflow could not have
produced it.** Workflow code is deterministic; `uuid.New()` is not available to
it. The only place that *can* mint a fresh value per attempt is the activity —
which is exactly the place whose values are not stable across attempts. The
trap is structural, not careless.

Rule of thumb worth keeping: **an idempotency key must be derived from the
identity of the work, not from the identity of the attempt.** Order id, request
id, workflow id plus step name — all fine. Anything generated at the moment of
the call — never.

## And the one you get for free

```go
ID: orderID,   // the order id IS the workflow id
WorkflowIDReusePolicy:    REJECT_DUPLICATE,   // a closed execution with this id
WorkflowIDConflictPolicy: FAIL,               // a running execution with this id
```

A double-clicked button, a retried POST or a redelivered queue message cannot
start a second saga. No dedupe table, no request cache. Naming the workflow
after the business object is a design decision that pays rent.

Two details the scenario demonstrates and most tutorials skip:

- **Reuse and conflict are different questions.** One is about ids that have
  finished, the other about ids that are still running. Neither has a safe
  default, because both are business rules: *may a cancelled order be re-placed
  under the same id?* is not a question a library can answer for you.
- **The Go SDK swallows the duplicate by default.** `ExecuteWorkflow` returns a
  handle to the already-running execution and **no error** unless you set
  `WorkflowExecutionErrorWhenAlreadyStarted: true`. The dedupe still works — but
  without that flag your API cannot tell the caller that their retry was a
  duplicate. A convenient default that quietly discards information is worth
  finding before it matters.

## The database detail

`idempotency_key TEXT UNIQUE`, and the column is nullable. In SQLite, in
PostgreSQL and in the SQL standard, **NULLs are distinct in a UNIQUE index** —
so rows with no key collide with nothing and you can insert a thousand of them.
That is not a quirk to work around; it is an accurate encoding of the rule. A
nullable key column protects exactly the rows that bothered to fill it in.

## Run it

```bash
./lab.sh run 02
```
