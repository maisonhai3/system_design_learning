package saga

import (
	"fmt"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

// defaultActivityOptions is the retry policy for every forward step.
//
// Four attempts over roughly two seconds is aggressive for a lab and wrong for
// production, where you want the backoff to outlast a deploy. What matters is
// that the policy is DATA, declared here, and not a for-loop somewhere in the
// business logic — which means you can look at one struct and know exactly how
// hard this system will hammer a struggling downstream.
func defaultActivityOptions() workflow.ActivityOptions {
	return workflow.ActivityOptions{
		// The whole activity, including retries, must finish within this.
		StartToCloseTimeout: 10 * time.Second,
		// How long a worker may go silent before the activity is declared lost.
		// Only meaningful for activities that actually heartbeat.
		HeartbeatTimeout: 5 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    200 * time.Millisecond,
			BackoffCoefficient: 2.0,
			MaximumInterval:    2 * time.Second,
			MaximumAttempts:    4,
		},
	}
}

// OrderWorkflow is the whole business process, written as if nothing ever fails
// — which is the claim durable execution actually makes. The process below has
// no state machine, no "resume from step 3" branch and no persistence code, and
// it survives the worker being killed at any line.
//
// Three rules govern this function, and breaking any of them breaks replay:
//
//  1. No I/O. Every external call goes through an activity.
//  2. No wall clock, no rand, no map iteration order, no goroutines. Use
//     workflow.Now, workflow.SideEffect, workflow.Go.
//  3. No changing the sequence of commands for histories that already exist.
//     That is scenario 05, and it is the tax you pay for the other two.
func OrderWorkflow(ctx workflow.Context, in OrderInput) (OrderState, error) {
	state := OrderState{OrderID: in.OrderID, Phase: PhaseReceived}
	log := workflow.GetLogger(ctx)

	// A query handler reads state straight out of the running workflow. There
	// is no orders table anywhere in this lab: for work that is still in
	// flight, the workflow is the read model. That removes the commonest
	// distributed-systems bug there is — the process and the row that is
	// supposed to describe it drifting apart.
	if err := workflow.SetQueryHandler(ctx, QueryState, func() (OrderState, error) {
		return state, nil
	}); err != nil {
		return state, err
	}

	ctx = workflow.WithActivityOptions(ctx, defaultActivityOptions())
	var a *Activities // nil receiver: the SDK resolves activities by name, and
	// using a typed nil here keeps the call sites refactor-safe.

	// Compensations are pushed as we go and run in reverse on failure. A stack,
	// not a cleanup function per error path: every error path then does the
	// same thing, and adding a step in the middle cannot forget to undo it.
	var comp compensations

	// ---------------------------------------------------------------- approve
	if in.AmountCents > ApprovalThresholdCents {
		state.Phase = PhaseAwaitingApproval
		approved, timedOut, err := waitForApproval(ctx, in)
		if err != nil {
			return fail(&state, err)
		}
		if timedOut {
			state.Phase = PhaseRejected
			state.Failure = "nobody approved in time"
			log.Info("approval timed out", "order", in.OrderID)
			return state, nil // not an error: "no answer" is a valid outcome
		}
		state.Approved = &approved
		if !approved {
			state.Phase = PhaseRejected
			state.Failure = "rejected by reviewer"
			return state, nil
		}
	}

	// ---------------------------------------------------------------- reserve
	var reservationID string
	err := workflow.ExecuteActivity(ctx, a.ReserveInventory, ReserveInput{
		OrderID: in.OrderID, SKU: in.SKU, Qty: in.Qty,
		IdempotencyKey: in.OrderID + "/reserve", KeyMode: in.IdempotencyMode,
	}).Get(ctx, &reservationID)
	if err != nil {
		// Nothing has happened yet, so there is nothing to undo.
		return fail(&state, err)
	}
	state.ReservationID, state.Phase = reservationID, PhaseReserved
	comp.push("release-inventory", func(ctx workflow.Context) error {
		return workflow.ExecuteActivity(ctx, a.ReleaseInventory, reservationID).Get(ctx, nil)
	})

	// ----------------------------------------------------------------- charge
	//
	// The idempotency key is computed HERE, in workflow code, from data that is
	// already in history. That is what makes it stable: every retry of the
	// activity, and every replay of this workflow after a crash, produces the
	// same string. A key generated inside the activity would be new on every
	// attempt and would protect nothing — see scenario 02, which charges a
	// customer twice to prove it.
	var charge ChargeResult
	err = workflow.ExecuteActivity(ctx, a.ChargePayment, ChargeInput{
		OrderID: in.OrderID, AmountCents: in.AmountCents,
		IdempotencyKey: in.OrderID + "/charge", KeyMode: in.IdempotencyMode,
	}).Get(ctx, &charge)
	if err != nil {
		return failAndCompensate(ctx, &state, &comp, err)
	}
	state.ChargeID, state.Phase = charge.ChargeID, PhaseCharged
	comp.push("refund-payment", func(ctx workflow.Context) error {
		return workflow.ExecuteActivity(ctx, a.RefundPayment, RefundInput{
			OrderID: in.OrderID, ChargeID: charge.ChargeID,
			IdempotencyKey: in.OrderID + "/refund", KeyMode: in.IdempotencyMode,
		}).Get(ctx, nil)
	})

	// ------------------------------------------------------------------- pack
	//
	// A sleep. It looks like time.Sleep and it is nothing like it: the worker
	// is not waiting, it has forgotten this workflow exists. A timer is a row
	// on the Temporal server, and when it fires the workflow is scheduled onto
	// whichever worker is alive then. That is why the same line works for six
	// seconds and for thirty days, and why killing the worker here costs
	// nothing — the naive orchestrator's time.Sleep dies with the process.
	if in.PackSeconds > 0 {
		state.Phase = PhasePacking
		if err := workflow.Sleep(ctx, time.Duration(in.PackSeconds)*time.Second); err != nil {
			return failAndCompensate(ctx, &state, &comp, err)
		}
	}

	// ------------------------------------------------------------------- ship
	var shipmentID string
	err = workflow.ExecuteActivity(ctx, a.CreateShipment, ShipInput{
		OrderID: in.OrderID, Address: in.Address,
		IdempotencyKey: in.OrderID + "/ship", KeyMode: in.IdempotencyMode,
	}).Get(ctx, &shipmentID)
	if err != nil {
		return failAndCompensate(ctx, &state, &comp, err)
	}
	state.ShipmentID, state.Phase = shipmentID, PhaseShipped

	state.Phase = PhaseCompleted
	log.Info("order completed", "order", in.OrderID, "charge", state.ChargeID, "shipment", shipmentID)
	return state, nil
}

// waitForApproval blocks until a signal arrives or the timer fires.
//
// The cost of being blocked here is zero: no goroutine, no connection, no row
// being polled. Between the signal and the timeout, this workflow exists only
// as history plus one timer. You can restart every worker you own and come
// back to it.
func waitForApproval(ctx workflow.Context, in OrderInput) (approved, timedOut bool, err error) {
	timeout := time.Duration(in.ApprovalTimeoutSeconds) * time.Second
	if in.ApprovalTimeoutSeconds == 0 {
		timeout = 72 * time.Hour
	}

	var got *ApprovalSignal
	workflow.Go(ctx, func(gctx workflow.Context) {
		ch := workflow.GetSignalChannel(gctx, SignalApproval)
		var sig ApprovalSignal
		ch.Receive(gctx, &sig)
		got = &sig
	})

	ok, err := workflow.AwaitWithTimeout(ctx, timeout, func() bool { return got != nil })
	if err != nil {
		return false, false, err
	}
	if !ok {
		return false, true, nil
	}
	return got.Approved, false, nil
}

// compensations is a stack of undo steps.
type compensations struct {
	names []string
	fns   []func(workflow.Context) error
}

func (c *compensations) push(name string, fn func(workflow.Context) error) {
	c.names = append(c.names, name)
	c.fns = append(c.fns, fn)
}

// run executes the undo steps in reverse order.
//
// It runs on a DISCONNECTED context, and that is not a detail. If this workflow
// is cancelled — someone runs `temporal workflow cancel`, or a parent gives up
// — the normal ctx is cancelled too, and every compensation would fail
// instantly, leaving the customer charged for an order nobody is shipping.
// Rollback has to be able to outlive the thing it is rolling back.
func (c *compensations) run(ctx workflow.Context, state *OrderState) {
	dctx, cancel := workflow.NewDisconnectedContext(ctx)
	defer cancel()
	dctx = workflow.WithActivityOptions(dctx, defaultActivityOptions())
	log := workflow.GetLogger(ctx)

	for i := len(c.fns) - 1; i >= 0; i-- {
		name := c.names[i]
		if err := c.fns[i](dctx); err != nil {
			// Keep going. Half a rollback is better than a quarter of one, and
			// the alternative — stopping at the first failure — guarantees the
			// steps after it are never even attempted.
			log.Error("compensation failed", "step", name, "err", err)
			state.CompensationErrors = append(state.CompensationErrors,
				fmt.Sprintf("%s: %v", name, err))
			continue
		}
		state.Compensated = append(state.Compensated, name)
	}
}

func fail(state *OrderState, err error) (OrderState, error) {
	state.Phase = PhaseFailed
	state.Failure = err.Error()
	return *state, err
}

func failAndCompensate(ctx workflow.Context, state *OrderState, comp *compensations, cause error) (OrderState, error) {
	state.Phase = PhaseCompensating
	state.Failure = cause.Error()
	comp.run(ctx, state)
	if len(state.CompensationErrors) > 0 {
		// The loudest thing the workflow can say. An order in this state needs
		// a person, and pretending otherwise is how money goes missing.
		state.Phase = PhaseStuck
	} else {
		state.Phase = PhaseFailed
	}
	return *state, cause
}
