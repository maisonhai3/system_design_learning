// Scenario 01 — kill the orchestrator halfway through, twice.
//
// Same services, same order, same moment of death. The only difference is who
// was holding the progress.
package main

import (
	"fmt"
	"time"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/lab"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

const packSeconds = 8

func main() {
	r := lab.NewRun("01 — the crash that does not matter",
		"Kill the process mid-saga and the naive order is gone with the customer's money still taken. "+
			"Kill it mid-saga under Temporal and the order resumes on the next worker — without re-charging, "+
			"because the completed steps are in history, not in the process.")
	l := lab.Open()
	r.Must(l.Reset(), "reset services")

	// ---------------------------------------------------------------- naive
	r.Step("naive mode: place an order, let it charge, then kill the process holding it")

	naiveID := lab.ID("naive_crash")
	_, err := l.CreateOrder(orders.CreateRequest{
		OrderID: naiveID, Customer: "alice", AmountCents: 1999,
		PackSeconds: packSeconds, Mode: "naive",
	})
	r.Must(err, "create naive order")

	st, ok := l.WaitForPhase(naiveID, 20*time.Second, saga.PhasePacking)
	r.Check("the naive order got as far as packing (money already taken)", ok)
	r.Note("phase=%s charge=%s", st.Phase, st.ChargeID)

	before, err := l.Ledger(naiveID)
	r.Must(err, "ledger")
	r.Check("the customer has been charged once", before.Charges == 1 && before.ChargedCents == 1999)

	r.Step("kill -9 the orders service (the naive orchestrator lives inside it)")
	r.Must(l.StopOrders(), "kill orders")
	r.Must(l.StartOrders(), "restart orders")
	r.Check("the orders service is back", l.WaitHealthy(l.Orders, 30*time.Second))

	// Give the restarted process more than enough time to do something. It
	// will not: there is nothing left that knows the order existed.
	time.Sleep(time.Duration(packSeconds) * time.Second)

	after, err := l.Ledger(naiveID)
	r.Must(err, "ledger")
	ships, err := l.Shipments(naiveID)
	r.Must(err, "shipments")
	_, statusErr := l.Status(naiveID)

	r.Anomaly("the charge is still on the customer's card", after.ChargedCents == 1999)
	r.Anomaly("nothing was ever shipped", len(ships) == 0)
	r.Anomaly("nothing was refunded either — no compensation ran", after.RefundedCents == 0)
	r.Anomaly("the order cannot even be looked up any more", statusErr != nil)
	r.Note("GET /orders/%s -> %v", naiveID, statusErr)
	r.Note("no error was returned to anyone. The order did not fail; it stopped existing.")

	// ------------------------------------------------------------- temporal
	r.Step("temporal mode: the same order, the same kill, at the same point")

	tempID := lab.ID("temporal_crash")
	_, err = l.CreateOrder(orders.CreateRequest{
		OrderID: tempID, Customer: "alice", AmountCents: 1999,
		PackSeconds: packSeconds, Mode: "temporal",
	})
	r.Must(err, "create temporal order")

	st, ok = l.WaitForPhase(tempID, 30*time.Second, saga.PhasePacking)
	r.Check("the temporal order got as far as packing", ok)
	chargeBefore := st.ChargeID
	r.Note("phase=%s charge=%s", st.Phase, chargeBefore)

	r.Step("kill -9 the worker while the workflow is mid-flight")
	r.Must(l.StopWorker(), "kill worker")

	// With no worker alive, ask the server what it thinks. It still has the
	// execution, and it still says RUNNING — because "running" is a fact about
	// the workflow, not about any process.
	down, err := l.Status(tempID)
	r.Must(err, "status with no worker")
	r.Check("with ZERO workers alive, the order is still RUNNING", down.Status == "RUNNING")
	r.Note("the workflow's state is on the server; the worker was only borrowing it")

	r.Step("start a worker again and watch it pick the order up mid-saga")
	resumed := time.Now()
	r.Must(l.StartWorker(), "start worker")

	done, ok := l.WaitForPhase(tempID, 90*time.Second, saga.PhaseCompleted)
	r.Fixed("the order finished by itself after the crash", ok && done.Phase == saga.PhaseCompleted)
	r.Measure("time from worker restart to COMPLETED", time.Since(resumed).Round(100*time.Millisecond))

	ledger, err := l.Ledger(tempID)
	r.Must(err, "ledger")
	ships, err = l.Shipments(tempID)
	r.Must(err, "shipments")

	r.Fixed("charged exactly once — the completed step was NOT redone", ledger.Charges == 1)
	r.Fixed("the charge id is the one from before the crash", done.ChargeID == chargeBefore)
	r.Fixed("shipped exactly once", len(ships) == 1)
	naiveShips, err := l.Shipments(naiveID)
	r.Must(err, "shipments")
	r.Measure("naive:    charges / shipments", fmt.Sprintf("%d / %d", after.Charges, len(naiveShips)))
	r.Measure("temporal: charges / shipments", fmt.Sprintf("%d / %d", ledger.Charges, len(ships)))

	r.Note("nothing in the workflow re-ran the charge because nothing in the workflow")
	r.Note("decides what to re-run: replay reads the result out of history and moves on.")

	r.Done()
}
