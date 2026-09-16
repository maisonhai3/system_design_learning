// Scenario 05 — the edit that breaks every order already in flight.
package main

import (
	"context"
	"flag"
	"os"
	"strings"
	"time"

	historypb "go.temporal.io/api/history/v1"
	"go.temporal.io/api/temporalproto"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/log"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/lab"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

func main() {
	save := flag.String("save", "", "also write the recorded history to this file (used as a test fixture)")
	flag.Parse()

	r := lab.NewRun("05 — workflow code is replayed, so editing it is a wire-format change",
		"Add a step to the top of a workflow and redeploy. New orders are fine. Every order that was "+
			"already running replays into a command that does not match its history and wedges. The same "+
			"edit behind GetVersion replays clean — at the cost of code you cannot delete until the last "+
			"old execution finishes.")
	l := lab.Open()
	r.Must(l.Reset(), "reset services")

	// ------------------------------------------------- record a real history
	r.Step("run one ordinary order to completion and take its history")
	id := lab.ID("history")
	_, err := l.CreateOrder(orders.CreateRequest{OrderID: id, Customer: "frank", AmountCents: 2500})
	r.Must(err, "create order")
	st, ok := l.WaitForPhase(id, 90*time.Second, saga.PhaseCompleted)
	r.Check("the order completed", ok && st.Phase == saga.PhaseCompleted)

	c, err := client.Dial(client.Options{HostPort: labhttp.Env("LAB_TEMPORAL", "localhost:7233")})
	r.Must(err, "dial temporal")
	defer c.Close()

	hist, err := fetchHistory(c, id)
	r.Must(err, "fetch history")
	r.Measure("events recorded for one order", len(hist.Events))
	r.Note("this is the whole order: every activity scheduled, every result, every timer.")
	r.Note("It is also the only reason a crashed workflow can carry on — and the only")
	r.Note("reason your new code has to agree with your old code about what happens next.")

	if *save != "" {
		raw, err := temporalproto.CustomJSONMarshalOptions{Indent: " "}.Marshal(hist)
		r.Must(err, "marshal history")
		r.Must(os.WriteFile(*save, raw, 0o644), "write history")
		r.Note("wrote %s (%d bytes) — `go test ./...` replays it with no server running", *save, len(raw))
	}

	logger := log.NewStructuredLogger(newQuietLogger())

	// ------------------------------------------------------ the control case
	r.Step("replay that history against the code that produced it")
	unchanged := worker.NewWorkflowReplayer()
	unchanged.RegisterWorkflow(saga.OrderWorkflow)
	err = unchanged.ReplayWorkflowHistory(logger, hist)
	r.Check("unchanged code replays cleanly", err == nil)
	if err != nil {
		r.Note("unexpected: %v", err)
	}

	// -------------------------------------------------------- the naive edit
	r.Step("now replay it against the version with a fraud check added at the top")
	broken := worker.NewWorkflowReplayer()
	// Registered under the ORIGINAL name: this is a redeploy of the same
	// workflow, which is what makes it dangerous. A new name would be a new
	// workflow and would bother nobody.
	broken.RegisterWorkflowWithOptions(saga.OrderWorkflowAddedStep,
		workflow.RegisterOptions{Name: "OrderWorkflow"})
	errBroken := broken.ReplayWorkflowHistory(logger, hist)

	r.Anomaly("the in-flight order can no longer make progress", errBroken != nil)
	if errBroken != nil {
		r.Note("%s", firstLine(errBroken.Error()))
	}
	r.Anomaly("...and the error is TMPRL1100 — Temporal's non-determinism code",
		errBroken != nil && isNondeterminism(errBroken.Error()))
	r.Note("TMPRL1100 is the code worth memorising. It never means the business logic")
	r.Note("is wrong; it means the code and the history disagree about what happened.")
	r.Note("Nothing about the edit was wrong. It was wrong FOR HISTORIES THAT ALREADY EXIST.")

	// ------------------------------------------------------ the guarded edit
	r.Step("the same edit, behind workflow.GetVersion")
	patched := worker.NewWorkflowReplayer()
	patched.RegisterWorkflowWithOptions(saga.OrderWorkflowAddedStepPatched,
		workflow.RegisterOptions{Name: "OrderWorkflow"})
	errPatched := patched.ReplayWorkflowHistory(logger, hist)

	r.Fixed("the old order replays cleanly against the new code", errPatched == nil)
	if errPatched != nil {
		r.Note("unexpected: %v", errPatched)
	}
	r.Note("old history has no version marker, so GetVersion returns DefaultVersion")
	r.Note("and the new branch is skipped. New orders take it. Old orders finish as they started.")
	r.Note("The bill: that branch cannot be deleted until the last pre-change execution ends.")
	r.Note("For a workflow with a 72-hour approval gate, that is three days of dead code")
	r.Note("you are not allowed to remove — and several such branches at once, in time.")

	r.Step("this replay check is a unit test, not an outage")
	r.Note("`go test ./internal/saga/` runs exactly what just ran, against a recorded")
	r.Note("history, with no server. That is the one thing that makes versioning survivable:")
	r.Note("you find out in CI instead of at 3am.")

	r.Done()
}

func fetchHistory(c client.Client, workflowID string) (*historypb.History, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	iter := c.GetWorkflowHistory(ctx, workflowID, "", false, 0)
	hist := &historypb.History{}
	for iter.HasNext() {
		ev, err := iter.Next()
		if err != nil {
			return nil, err
		}
		hist.Events = append(hist.Events, ev)
	}
	return hist, nil
}

// isNondeterminism looks for TMPRL1100, which is the error code Temporal
// attaches to every flavour of "the replayed code did not issue the command
// history says it issued". The wording varies with where the divergence is
// noticed — an activity id that maps to nothing, a command in the wrong
// position, a duplicate command — and the code does not.
func isNondeterminism(s string) bool {
	return strings.Contains(s, "TMPRL1100")
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	if len(s) > 160 {
		s = s[:160] + "..."
	}
	return s
}
