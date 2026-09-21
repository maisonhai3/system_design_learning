package saga_test

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/mock"
	"go.temporal.io/sdk/log"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

// These tests run the real workflow with mocked activities, on an in-memory
// test environment. No Temporal server, no services, no Docker — and no
// sleeping: the environment skips time, so the 72-hour approval timeout below
// fires in about a millisecond.
//
// That is a bigger deal than it sounds. The reason distributed-systems bugs
// survive into production is that the failure paths are expensive to reach —
// you cannot make payments time out on demand, and you cannot wait three days.
// Here both are ordinary test cases.

func newEnv(t *testing.T) (*testsuite.TestWorkflowEnvironment, *saga.Activities) {
	t.Helper()
	var suite testsuite.WorkflowTestSuite
	suite.SetLogger(log.NewStructuredLogger(slog.New(slog.NewTextHandler(io.Discard, nil))))
	env := suite.NewTestWorkflowEnvironment()
	acts := &saga.Activities{}
	env.RegisterActivity(acts)
	return env, acts
}

func baseInput() saga.OrderInput {
	return saga.OrderInput{
		OrderID: "ord_test", Customer: "alice", SKU: "WIDGET-1", Qty: 1,
		AmountCents: 1999, Address: "1 Example Street",
	}
}

func TestOrderWorkflow_HappyPath(t *testing.T) {
	env, acts := newEnv(t)
	env.OnActivity(acts.ReserveInventory, mock.Anything, mock.Anything).Return("res_1", nil)
	env.OnActivity(acts.ChargePayment, mock.Anything, mock.Anything).
		Return(saga.ChargeResult{ChargeID: "chg_1"}, nil)
	env.OnActivity(acts.CreateShipment, mock.Anything, mock.Anything).Return("shp_1", nil)

	env.ExecuteWorkflow(saga.OrderWorkflow, baseInput())

	if !env.IsWorkflowCompleted() {
		t.Fatal("workflow did not complete")
	}
	if err := env.GetWorkflowError(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var out saga.OrderState
	if err := env.GetWorkflowResult(&out); err != nil {
		t.Fatal(err)
	}
	if out.Phase != saga.PhaseCompleted {
		t.Errorf("phase = %q, want COMPLETED", out.Phase)
	}
	if out.ChargeID != "chg_1" || out.ShipmentID != "shp_1" {
		t.Errorf("unexpected ids: %+v", out)
	}
	env.AssertExpectations(t)
}

// The claim: when shipping fails for good, the undo steps run in the reverse of
// the order they were taken — refund before release — and both actually run.
func TestOrderWorkflow_CompensatesInReverseOrder(t *testing.T) {
	env, acts := newEnv(t)

	var mu sync.Mutex
	var calls []string
	record := func(name string) { mu.Lock(); calls = append(calls, name); mu.Unlock() }

	env.OnActivity(acts.ReserveInventory, mock.Anything, mock.Anything).
		Return(func(context.Context, saga.ReserveInput) (string, error) {
			record("reserve")
			return "res_1", nil
		})
	env.OnActivity(acts.ChargePayment, mock.Anything, mock.Anything).
		Return(func(context.Context, saga.ChargeInput) (saga.ChargeResult, error) {
			record("charge")
			return saga.ChargeResult{ChargeID: "chg_1"}, nil
		})
	env.OnActivity(acts.CreateShipment, mock.Anything, mock.Anything).
		Return(func(context.Context, saga.ShipInput) (string, error) {
			record("ship")
			return "", errors.New("carrier is down")
		})
	env.OnActivity(acts.RefundPayment, mock.Anything, mock.Anything).
		Return(func(context.Context, saga.RefundInput) error {
			record("refund")
			return nil
		})
	env.OnActivity(acts.ReleaseInventory, mock.Anything, mock.Anything).
		Return(func(context.Context, string) error {
			record("release")
			return nil
		})

	env.ExecuteWorkflow(saga.OrderWorkflow, baseInput())

	if !env.IsWorkflowCompleted() {
		t.Fatal("workflow did not complete")
	}
	if env.GetWorkflowError() == nil {
		t.Fatal("expected the workflow to report the shipping failure")
	}

	mu.Lock()
	defer mu.Unlock()
	got := strings.Join(dedupe(calls), ",")
	const want = "reserve,charge,ship,refund,release"
	if got != want {
		t.Errorf("call order = %q, want %q", got, want)
	}
}

// The claim: a business rejection (4xx) is not retried. Getting this wrong is
// invisible in tests that only check the final state — the order fails either
// way — and very visible in production, where it multiplies load on a service
// that is already saying no.
func TestOrderWorkflow_BusinessRejectionIsNotRetried(t *testing.T) {
	env, acts := newEnv(t)

	var attempts int
	var mu sync.Mutex
	env.OnActivity(acts.ReserveInventory, mock.Anything, mock.Anything).
		Return(func(context.Context, saga.ReserveInput) (string, error) {
			mu.Lock()
			attempts++
			mu.Unlock()
			return "", temporal.NewNonRetryableApplicationError(
				"reserve inventory rejected: only 1 left of SCARCE-1", "insufficient_stock", nil)
		})

	in := baseInput()
	in.SKU, in.Qty = "SCARCE-1", 5
	env.ExecuteWorkflow(saga.OrderWorkflow, in)

	if env.GetWorkflowError() == nil {
		t.Fatal("expected the order to fail")
	}
	mu.Lock()
	defer mu.Unlock()
	if attempts != 1 {
		t.Errorf("reserve attempted %d times, want 1 — a rejection is not a fault", attempts)
	}
}

// The claim: a 72-hour human approval gate is an ordinary unit test.
func TestOrderWorkflow_ApprovalTimeout_SkipsThreeDays(t *testing.T) {
	env, acts := newEnv(t)
	env.OnActivity(acts.ReserveInventory, mock.Anything, mock.Anything).Return("res_1", nil).Maybe()
	env.OnActivity(acts.ChargePayment, mock.Anything, mock.Anything).
		Return(saga.ChargeResult{ChargeID: "chg_1"}, nil).Maybe()

	in := baseInput()
	in.AmountCents = saga.ApprovalThresholdCents + 1 // needs a human
	// ApprovalTimeoutSeconds left at 0, so the workflow uses its 72h default.

	started := time.Now()
	env.ExecuteWorkflow(saga.OrderWorkflow, in)
	elapsed := time.Since(started)

	if !env.IsWorkflowCompleted() {
		t.Fatal("workflow did not complete")
	}
	var out saga.OrderState
	if err := env.GetWorkflowResult(&out); err != nil {
		t.Fatal(err)
	}
	if out.Phase != saga.PhaseRejected {
		t.Errorf("phase = %q, want REJECTED", out.Phase)
	}
	if out.ChargeID != "" {
		t.Errorf("charged %q on an order nobody approved", out.ChargeID)
	}
	// The wall-clock assertion is the point of the test, not a performance
	// check: three simulated days cost single-digit milliseconds.
	if elapsed > 10*time.Second {
		t.Errorf("took %v of real time to skip 72 simulated hours", elapsed)
	}
	t.Logf("72 simulated hours elapsed in %v of real time", elapsed.Round(time.Millisecond))
}

func TestOrderWorkflow_ApprovalSignalCompletesTheOrder(t *testing.T) {
	env, acts := newEnv(t)
	env.OnActivity(acts.ReserveInventory, mock.Anything, mock.Anything).Return("res_1", nil)
	env.OnActivity(acts.ChargePayment, mock.Anything, mock.Anything).
		Return(saga.ChargeResult{ChargeID: "chg_1"}, nil)
	env.OnActivity(acts.CreateShipment, mock.Anything, mock.Anything).Return("shp_1", nil)

	// A reviewer who takes one simulated hour to answer.
	env.RegisterDelayedCallback(func() {
		env.SignalWorkflow(saga.SignalApproval, saga.ApprovalSignal{Approved: true, By: "reviewer"})
	}, time.Hour)

	in := baseInput()
	in.AmountCents = saga.ApprovalThresholdCents + 1
	env.ExecuteWorkflow(saga.OrderWorkflow, in)

	var out saga.OrderState
	if err := env.GetWorkflowResult(&out); err != nil {
		t.Fatal(err)
	}
	if out.Phase != saga.PhaseCompleted {
		t.Fatalf("phase = %q, want COMPLETED", out.Phase)
	}
	if out.Approved == nil || !*out.Approved {
		t.Error("the approval was not recorded on the order")
	}
}

func TestOrderWorkflow_RejectionSkipsEverything(t *testing.T) {
	env, _ := newEnv(t)
	env.RegisterDelayedCallback(func() {
		env.SignalWorkflow(saga.SignalApproval, saga.ApprovalSignal{Approved: false, By: "reviewer"})
	}, time.Minute)

	in := baseInput()
	in.AmountCents = saga.ApprovalThresholdCents + 1
	env.ExecuteWorkflow(saga.OrderWorkflow, in)

	var out saga.OrderState
	if err := env.GetWorkflowResult(&out); err != nil {
		t.Fatal(err)
	}
	if out.Phase != saga.PhaseRejected {
		t.Errorf("phase = %q, want REJECTED", out.Phase)
	}
	// No activity mocks were set at all: if the workflow touched a service
	// after a rejection, this test would fail on an unmocked activity.
}

func dedupe(in []string) []string {
	out := in[:0:0]
	for i, s := range in {
		if i == 0 || in[i-1] != s {
			out = append(out, s)
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// Replay tests: the CI gate that makes workflow versioning survivable.
//
// testdata/order_history.json is a real history, recorded from a real server by
// `./lab.sh run 05`. These tests replay it against three versions of the
// workflow and assert which ones an already-running order would survive.
//
// A repository with long-lived workflows should replay a history from
// production here, on every pull request. It is the only check that catches
// "this edit is fine" when the edit is fine for new work and fatal for old.

const historyFixture = "../../testdata/order_history.json"

func replayLogger() log.Logger {
	return log.NewStructuredLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func TestReplay_UnchangedCodeIsCompatible(t *testing.T) {
	r := worker.NewWorkflowReplayer()
	r.RegisterWorkflow(saga.OrderWorkflow)
	if err := r.ReplayWorkflowHistoryFromJSONFile(replayLogger(), historyFixture); err != nil {
		t.Fatalf("the current code cannot replay its own history: %v", err)
	}
}

func TestReplay_AddingAStepBreaksRunningOrders(t *testing.T) {
	r := worker.NewWorkflowReplayer()
	r.RegisterWorkflowWithOptions(saga.OrderWorkflowAddedStep,
		workflow.RegisterOptions{Name: "OrderWorkflow"})

	err := r.ReplayWorkflowHistoryFromJSONFile(replayLogger(), historyFixture)
	if err == nil {
		t.Fatal("expected a non-determinism error: an activity was inserted before a recorded one")
	}
	if !strings.Contains(err.Error(), "TMPRL1100") {
		t.Errorf("expected TMPRL1100 (non-determinism), got: %v", err)
	}
}

func TestReplay_GetVersionKeepsRunningOrdersAlive(t *testing.T) {
	r := worker.NewWorkflowReplayer()
	r.RegisterWorkflowWithOptions(saga.OrderWorkflowAddedStepPatched,
		workflow.RegisterOptions{Name: "OrderWorkflow"})

	if err := r.ReplayWorkflowHistoryFromJSONFile(replayLogger(), historyFixture); err != nil {
		t.Fatalf("the guarded change should replay cleanly, got: %v", err)
	}
}
