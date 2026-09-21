package saga

import (
	"go.temporal.io/sdk/workflow"
)

// This file is scenario 05: what happens when you edit a workflow that is
// already running somewhere.
//
// Both functions below make the SAME change — a fraud check before the
// inventory reservation. One of them breaks every order that was mid-flight
// when it deployed. The other does not. Nothing about the change looks risky;
// the risk is in what workflow code IS.
//
// A workflow function is not executed once. It is re-executed from the top,
// from history, every time a worker picks the workflow up — after a crash,
// after a timer, after a signal, months later. The SDK checks each command the
// replayed code issues against the command recorded in history. Issue a
// different one and it has no way to reconcile them, so it refuses: the
// workflow stops making progress rather than silently doing something nobody
// wrote.

// OrderWorkflowAddedStep is the edit as anyone would write it.
//
// Deployed while orders are in flight, every one of them replays, reaches this
// line, issues ScheduleActivityTask(FraudCheck) where history says
// ScheduleActivityTask(ReserveInventory), and wedges. The orders that mattered
// are the ones that were already running, which is exactly the set that never
// appears in a test.
func OrderWorkflowAddedStep(ctx workflow.Context, in OrderInput) (OrderState, error) {
	actx := workflow.WithActivityOptions(ctx, defaultActivityOptions())
	var a *Activities
	var passed bool
	if err := workflow.ExecuteActivity(actx, a.FraudCheck, in.OrderID).Get(actx, &passed); err != nil {
		return OrderState{OrderID: in.OrderID, Phase: PhaseFailed, Failure: err.Error()}, err
	}
	return OrderWorkflow(ctx, in)
}

// OrderWorkflowAddedStepPatched is the same edit, guarded.
//
// GetVersion writes a marker into history the first time a NEW execution
// reaches it. Replaying an OLD history, there is no marker, so it returns
// DefaultVersion and the new branch is skipped — the replayed code issues the
// same commands it issued the first time. New orders get the fraud check; old
// orders finish the way they started.
//
// The cost is honest and worth knowing before you choose this architecture:
// this branch cannot be deleted until every execution that predates it has
// finished. For a workflow with a 30-day timer, that is 30 days of carrying
// code you have already replaced — and in a long-lived process, several such
// branches at once.
func OrderWorkflowAddedStepPatched(ctx workflow.Context, in OrderInput) (OrderState, error) {
	v := workflow.GetVersion(ctx, "add-fraud-check", workflow.DefaultVersion, 1)
	if v != workflow.DefaultVersion {
		actx := workflow.WithActivityOptions(ctx, defaultActivityOptions())
		var a *Activities
		var passed bool
		if err := workflow.ExecuteActivity(actx, a.FraudCheck, in.OrderID).Get(actx, &passed); err != nil {
			return OrderState{OrderID: in.OrderID, Phase: PhaseFailed, Failure: err.Error()}, err
		}
	}
	return OrderWorkflow(ctx, in)
}
