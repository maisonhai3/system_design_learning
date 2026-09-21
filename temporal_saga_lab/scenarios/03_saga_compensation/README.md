# Compensation is not rollback

**The claim you are practising:** *"Across four services there is no `ROLLBACK`.
A saga cannot make things un-happen; it can only add opposite things, in reverse
order, on a context that survives cancellation — and when one of those fails,
the only honest move left is to stop claiming success."*

## Why there is no transaction to roll back

Three services, three databases, three files that no single process has open at
the same time. `internal/store` enforces this physically: payments literally
cannot read inventory's table.

That constraint is the reason this lab exists. If these three shared a database,
the correct design would be one transaction and none of the machinery below
would be justified. Every saga is a confession that a transaction was not
available.

## What the scenario forces

The carrier is broken permanently, so shipping fails *after* the money is taken.

```
reserve ✅ ──▶ charge ✅ ──▶ ship ✗✗✗✗   (4 attempts, then give up)
                                 │
   release ◀── refund ◀──────────┘       compensations, in reverse
```

Measured:

| | |
|---|---|
| charges / refunds on the statement | **1 / 1** |
| net taken from the customer | **0** |
| compensations, in order | `refund-payment -> release-inventory` |
| stock | back where it started |

The charge row is still there. It always will be. The customer's statement shows
two entries for an order that never existed — and if they look at it mid-rollback
they see one. "Eventually consistent" is not an abstraction here; it is a number
on somebody's bank statement for a few seconds.

## Three details in the code that are not decoration

**Reverse order.** `compensations` is a stack. Releasing the stock before
refunding the card would open a window where a second order can buy an item the
first customer is still paying for.

**A disconnected context.**

```go
dctx, cancel := workflow.NewDisconnectedContext(ctx)
```

If the workflow is cancelled — someone runs `temporal workflow cancel`, or a
parent gives up — the normal context is cancelled too, and every compensation
would fail instantly, leaving the customer charged for an order nobody is
shipping. **Rollback has to be able to outlive the thing it is rolling back.**

**Compensations are idempotent by state, not by key.** `release` on an
already-released reservation returns 200, not an error. Compensations run on the
worst day, from a retry loop, possibly twice — so "already done" must mean
success. A compensation that errors on a repeat sends the rollback down a second
failure path at the exact moment you can least afford one.

## The second half: when the rollback itself fails

The scenario then breaks the refund endpoint too — and only the refund endpoint,
so the charge still works and the saga still gets far enough to need one.

| | |
|---|---|
| net still taken from the customer | **the full amount** |
| phase | `STUCK`, not `FAILED` |
| compensation errors | `refund-payment: ... http 500` |
| inventory | **still released** |

Three things worth taking from that table:

1. **No orchestrator can fix this.** The money is gone and the only system that
   can move it back is refusing to. Durable execution does not change that.
2. **It did not stop at the first failure.** The rollback loop continues past a
   failing step, so the stock still came back. Half a rollback beats a quarter
   of one, and "abort on first error" guarantees the later steps are never even
   attempted.
3. **It does not report success.** `STUCK` is a distinct phase from `FAILED`,
   the failing step is named in the result, and the whole thing is in a history
   an alert can query. The most valuable property of a failed saga is that it is
   *loud and specific*, because from here on a person is doing the work.

The naive orchestrator has the same rollback logic — and records the failure in
a map in a process that will be redeployed this afternoon.

## Run it

```bash
./lab.sh run 03
```

To watch it by hand:

```bash
./lab.sh chaos shipping before_commit -1      # break the carrier for good
./lab.sh order 7500
./lab.sh ledger <order-id>                    # charged, then refunded, net 0
./lab.sh chaos shipping off
```
