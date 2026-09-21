# Workflow code is replayed, so editing it is a wire-format change

**The claim you are practising:** *"A workflow function is not executed once. It
is re-executed from the top, from history, every time a worker picks the
workflow up. That is what buys you scenario 01 — and it means an ordinary-looking
edit can break every order that is already in flight."*

## The mechanism, which is also the hazard

After a crash, a timer, or a signal, a worker rebuilds the workflow's state by
running your function again and matching each command it issues against the
command recorded in history. Same sequence → the SDK returns the recorded
results instantly and execution continues from the first unfinished step.
Different sequence → it has no way to reconcile them, so it refuses.

That refusal has a code: **`TMPRL1100`**. It is worth memorising, because it
never means your business logic is wrong. It means *the code and the history
disagree about what already happened*.

## The edit

Six months in, someone adds a fraud check before the inventory reservation.
Reasonable change, reviewed, tested, green.

```go
func OrderWorkflowAddedStep(ctx workflow.Context, in OrderInput) (OrderState, error) {
    workflow.ExecuteActivity(actx, a.FraudCheck, in.OrderID).Get(actx, &passed)   // new
    return OrderWorkflow(ctx, in)
}
```

The scenario records a real history from a real order, then replays it against
that code:

```
[TMPRL1100] lookup failed for scheduledEventID to activityID: scheduleEventID: 11, activityID: 11
```

New orders are fine. Every order that was mid-flight when this deployed is
wedged — and that is exactly the set that never appears in a test, because tests
start from nothing.

## The fix, and its bill

```go
v := workflow.GetVersion(ctx, "add-fraud-check", workflow.DefaultVersion, 1)
if v != workflow.DefaultVersion {
    // the new step
}
```

`GetVersion` writes a marker into history the first time a **new** execution
reaches it. Replaying an **old** history there is no marker, so it returns
`DefaultVersion`, the branch is skipped, and the replayed code issues exactly
the commands it issued the first time. New orders get the fraud check; old
orders finish the way they started.

Now the part the tutorials tend to leave out. **That branch cannot be deleted
until every execution that predates it has finished.** For a workflow with a
72-hour approval gate, that is three days of carrying code you have already
replaced — and in a long-lived process, several such branches at once, each with
a name you have to keep straight. `GetVersion` is not free; it is deferred
maintenance with a due date.

## Which edits are safe

| edit | safe on running executions? |
|---|---|
| activity *implementation* — the body of `ChargePayment` | **yes.** Activities are not replayed, only their recorded results |
| adding / removing / reordering an `ExecuteActivity` call | no |
| changing a timer's duration, adding a `workflow.Sleep` | no |
| `if` on a value that is not in history (wall clock, `rand`, env var, map order) | no — and it breaks non-deterministically, which is worse |
| adding a field to an activity's input struct | usually yes, if the codec tolerates it — this is a wire-format question, not a Go one |
| renaming the workflow | it is a different workflow; old ones keep running on old code |

Reading that table the other way round is the useful move: **the constraint is
the same one you already accept at any serialisation boundary.** You would not
reorder fields in a protobuf message and expect old readers to cope. Workflow
code is a wire format whose reader is your own code, six months ago.

## The reason this is survivable

```bash
./lab.sh test        # runs the replay checks, no server, no docker, ~1s
```

`internal/saga/workflow_test.go` replays a committed history
(`testdata/order_history.json`, recorded by this scenario) against all three
versions of the workflow and asserts which ones an already-running order would
survive.

A repository with long-lived workflows should do this in CI, against a history
pulled from production, on every pull request. It is the only check that catches
"this edit is fine" when the edit is fine for new work and fatal for old — and
it is the difference between finding out in CI and finding out at 3am.

## Run it

```bash
./lab.sh run 05
```

To re-record the fixture after changing the workflow on purpose:

```bash
go run ./scenarios/05_determinism -save testdata/order_history.json
```
