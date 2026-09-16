# The wait that outlives the process

**The claim you are practising:** *"A workflow blocked for three days on a human
costs nothing to keep alive and survives every deploy in between — because the
wait is a row and a timer on a server, not a goroutine on a box."*

## The shape

Orders over $500 need a person to say yes:

```go
ok, err := workflow.AwaitWithTimeout(ctx, 72*time.Hour, func() bool {
    return got != nil            // set by a signal handler
})
```

Two outcomes, both ordinary: a signal arrives, or the timer fires. While neither
has happened, this workflow exists as history plus one timer. No goroutine, no
connection, no row being polled, no worker holding anything.

## What the scenario proves

1. **Nothing is charged and no stock is held while it waits.** The gate is the
   first step, so a rejected order costs nothing to undo. Where you put a human
   in a saga is a design decision with a rollback bill attached.
2. **`kill -9` every worker, bring one back — the order is still waiting.** It
   resumes on a worker that has never seen it. Nothing was restored, because
   nothing was lost.
3. **The approval then lands and the order completes.** A signal is written to
   history by the server before any worker sees it; whether a worker is running
   at that instant is not the sender's problem.
4. **A second order nobody approves times out by itself**, and ends `REJECTED`
   with no charge and no reservation. The only difference between this and a
   72-hour hold is the number `5`.

Then the same wait in the naive orchestrator, across a restart of the orders
service:

```
POST /orders/ord_.../approve  ->  404 no such order
```

The reviewer did their job. The system lost the question. Nobody was told —
not the reviewer, not the customer, not an alert.

## Where the state lives

There is no `orders` table in this lab. Not a small one: none.

```go
workflow.SetQueryHandler(ctx, "state", func() (OrderState, error) { return state, nil })
```

`GET /orders/{id}` queries the running workflow. For work that is still in
flight, the workflow **is** the read model, and that removes one of the most
common bugs in this shape of system: the process and the row that is supposed to
describe it drifting apart, because updating the row is a second write that can
fail on its own.

The tradeoff is real and worth stating. Workflow state is not a database you can
`SELECT ... WHERE status = 'AWAITING_APPROVAL' ORDER BY amount DESC` — Temporal's
visibility store indexes a handful of attributes, not your domain. Most systems
end up projecting completed orders into a normal table for reporting, and keeping
only *in-flight* state in the workflow. "The workflow is the read model" holds
until someone in finance needs a join.

## The API detail worth copying

`GET /orders/{id}` returns two different fields on purpose:

- `phase` — what the business thinks: `AWAITING_APPROVAL`, `PACKING`, `STUCK`.
- `status` — what the engine knows: `RUNNING`, `COMPLETED`, `FAILED`.

A system that conflates them cannot tell "still working" from "gave up", which
is precisely the distinction an on-call engineer is trying to make at 3am.

## Run it

```bash
./lab.sh run 04
```

By hand:

```bash
curl -s -X POST localhost:8110/orders -H 'content-type: application/json' \
  -d '{"customer":"you","amount_cents":99900}'        # over $500
./lab.sh order-status <id>                            # AWAITING_APPROVAL
./lab.sh approve <id> true
```

Open <http://localhost:8233> while it waits. The workflow is `Running`, its last
event is `WorkflowTaskCompleted`, and the pending timer is listed with its
deadline. That is the entire cost of the wait.
