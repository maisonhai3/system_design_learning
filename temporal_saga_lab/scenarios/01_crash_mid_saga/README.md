# The crash that does not matter

**The claim you are practising:** *"Durable execution is not a retry library. It
is a state machine that outlives the process, and the thing it saves you from is
not an error — it is the absence of one."*

## The setup

The same order, twice, against the same three services. Both orchestrators get
as far as PACKING — inventory held, card charged, nothing shipped — and then the
process holding them is killed with `kill -9`.

```
                 reserve ──▶ charge ──▶ [ pack 8s ] ──▶ ship
                                            ▲
                                            │
                                      kill -9 here
```

| | naive | temporal |
|---|---|---|
| where the progress was | a `map[string]*OrderState` in the orders service | the Temporal server's history |
| what the kill destroyed | the order | one process |
| after restart | `GET /orders/{id}` → **404** | resumes, ships, completes |
| charges | 1 | 1 |
| shipments | **0, for ever** | 1 |

## The part that is easy to miss

The recovered order is charged **once**, and the charge id is the same string it
had before the crash.

Replay does not re-run completed activities. It reads their results out of
history and carries on from the first thing that had not finished. This is worth
dwelling on, because "it retries from where it left off" is a much weaker claim
than what actually happens: *the workflow function runs from the top again, and
every call that already has an answer returns that answer immediately, without
touching the network.*

That is also why workflow code must be deterministic, which is scenario 05's
bill for this scenario's benefit.

## The other easy thing to miss

With **zero workers alive**, the order is still `RUNNING`:

```bash
./lab.sh order-status ord_...
{"phase":"RUNNING","status":"RUNNING", ...}
```

Nothing is executing it. "Running" is a fact about the workflow, not about a
process — the same way a row in a database is not running on anything. The
worker was borrowing the work, not holding it.

## Why the naive version is not a strawman

Read `internal/naive/naive.go`. It has retries with exponential backoff, it
distinguishes retryable faults from business rejections, it compensates in
reverse order, and it supports the human approval gate. It is roughly the same
length as the workflow. For the happy path and for ordinary failures it does the
same job.

Its single defect is that the answer to "how far did order X get?" lives in one
process's memory. And notice what that defect costs: not an error, not an alert,
not a stack trace. The order simply stops existing, with the money already
taken, and **nobody is told**, because the component that would have reported it
is the component that died.

The general form is worth carrying out of this lab: *state that only exists
inside a running process is state you have decided to lose, and the loss is
silent.* Temporal is one answer. A durable state machine with a scheduler poking
it is another. Writing the saga's progress to a database and having a cron job
resume orphans is a third. Choosing none of them is also a choice, and it is the
one most order pipelines ship with.

## Run it

```bash
./lab.sh run 01
```

Then open the Temporal UI at <http://localhost:8233>, find the completed order,
and read the event history top to bottom. The gap where the worker was dead is
visible as a pause between `TimerFired` and the next `WorkflowTaskStarted` —
and nothing else in the history knows it happened.
