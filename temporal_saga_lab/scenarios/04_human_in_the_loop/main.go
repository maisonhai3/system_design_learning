// Scenario 04 — a workflow waiting three days costs nothing and survives a deploy.
package main

import (
	"time"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/lab"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

// Above saga.ApprovalThresholdCents ($500), a human has to say yes.
const bigOrder = 99900

func main() {
	r := lab.NewRun("04 — the wait that outlives the process",
		"A large order blocks on a human. Under Temporal the wait is a row and a timer on the server: "+
			"kill every worker, deploy new code, come back — the order is still waiting and the approval "+
			"still lands. The naive version is a goroutine parked on a channel, and a restart loses it "+
			"so completely that the approval gets a 404.")
	l := lab.Open()
	r.Must(l.Reset(), "reset services")

	// --------------------------------------------------- temporal: survives
	r.Step("place a $999 order — over the approval threshold")
	id := lab.ID("awaiting")
	_, err := l.CreateOrder(orders.CreateRequest{
		OrderID: id, Customer: "erin", AmountCents: bigOrder,
		ApprovalTimeoutSeconds: 600,
	})
	r.Must(err, "create order")

	st, ok := l.WaitForPhase(id, 30*time.Second, saga.PhaseAwaitingApproval)
	r.Check("the order is waiting for a human", ok && st.Phase == saga.PhaseAwaitingApproval)

	ledger, err := l.Ledger(id)
	r.Must(err, "ledger")
	res, err := l.Reservations(id)
	r.Must(err, "reservations")
	r.Check("nothing has been charged yet", ledger.Charges == 0)
	r.Check("no stock has been held yet", len(res) == 0)
	r.Note("the gate is the first step, so a rejected order costs nothing to undo")

	r.Step("kill -9 the worker, wait, bring it back — as a deploy would")
	r.Must(l.StopWorker(), "kill worker")
	time.Sleep(3 * time.Second)
	r.Must(l.StartWorker(), "start worker")

	back := lab.Wait(60*time.Second, func() bool {
		s, err := l.Status(id)
		return err == nil && s.Phase == saga.PhaseAwaitingApproval
	})
	r.Fixed("the order is still waiting, on a worker that has never seen it before", back)
	r.Note("no goroutine was restored. There was no goroutine: there was a timer on the server.")

	r.Step("approve it")
	r.Must(l.Approve(id, true), "approve")
	done, ok := l.WaitForPhase(id, 60*time.Second, saga.PhaseCompleted)
	r.Fixed("the approval landed and the order completed", ok && done.Phase == saga.PhaseCompleted)

	ledger, err = l.Ledger(id)
	r.Must(err, "ledger")
	ships, err := l.Shipments(id)
	r.Must(err, "shipments")
	r.Fixed("charged once, shipped once", ledger.Charges == 1 && len(ships) == 1)

	// ------------------------------------------------- temporal: the timeout
	r.Step("a second big order that nobody ever approves (timeout: 5s instead of 72h)")
	lateID := lab.ID("never_approved")
	_, err = l.CreateOrder(orders.CreateRequest{
		OrderID: lateID, Customer: "erin", AmountCents: bigOrder,
		ApprovalTimeoutSeconds: 5,
	})
	r.Must(err, "create order")

	rejected, ok := l.WaitForPhase(lateID, 60*time.Second, saga.PhaseRejected)
	r.Fixed("the timer fired and the order was rejected on its own", ok && rejected.Phase == saga.PhaseRejected)
	r.Note("reason: %s", rejected.Failure)

	ledger, err = l.Ledger(lateID)
	r.Must(err, "ledger")
	res, err = l.Reservations(lateID)
	r.Must(err, "reservations")
	r.Fixed("no money moved and no stock was held", ledger.Charges == 0 && len(res) == 0)
	r.Note("the only difference between this and a 72-hour hold is the number 5.")
	r.Note("Nothing polled. Nothing was kept warm. A timer fired on a server.")

	// ------------------------------------------------------ naive: does not
	r.Step("the same wait, in the naive orchestrator, across a restart")
	naiveID := lab.ID("naive_awaiting")
	_, err = l.CreateOrder(orders.CreateRequest{
		OrderID: naiveID, Customer: "erin", AmountCents: bigOrder,
		ApprovalTimeoutSeconds: 600, Mode: "naive",
	})
	r.Must(err, "create naive order")

	st, ok = l.WaitForPhase(naiveID, 30*time.Second, saga.PhaseAwaitingApproval)
	r.Check("the naive order is waiting for a human too", ok)

	r.Must(l.StopOrders(), "kill orders")
	r.Must(l.StartOrders(), "restart orders")
	r.Check("the orders service is back", l.WaitHealthy(l.Orders, 30*time.Second))

	approveErr := l.Approve(naiveID, true)
	_, statusErr := l.Status(naiveID)
	r.Anomaly("the approval has nowhere to go", approveErr != nil)
	r.Anomaly("the order the reviewer was emailed about no longer exists", statusErr != nil)
	r.Note("POST /orders/%s/approve -> %v", naiveID, approveErr)
	r.Note("the reviewer did their job. The system lost the question.")

	r.Done()
}
