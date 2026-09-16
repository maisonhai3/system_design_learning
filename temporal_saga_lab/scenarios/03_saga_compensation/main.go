// Scenario 03 — there is no ROLLBACK across four services.
package main

import (
	"strconv"
	"strings"
	"time"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/lab"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

const amount = 7500

func main() {
	r := lab.NewRun("03 — compensation is not rollback",
		"Shipping fails after the money is taken. A saga cannot undo the charge, only add an opposite "+
			"one, in reverse order, on a context that survives cancellation. And when a compensation "+
			"itself fails, the only honest thing left to do is say so.")
	l := lab.Open()
	r.Must(l.Reset(), "reset services")

	stock0, err := l.Stock()
	r.Must(err, "stock")

	// ------------------------------------------------- the rollback that works
	r.Step("break the carrier permanently, then place an order")
	r.Must(l.Chaos("shipping", "before_commit", -1, 0), "arm chaos")

	id := lab.ID("compensated")
	_, err = l.CreateOrder(orders.CreateRequest{OrderID: id, Customer: "dan", AmountCents: amount, Qty: 2})
	r.Must(err, "create order")

	st, ok := l.WaitForPhase(id, 90*time.Second, saga.PhaseFailed, saga.PhaseStuck)
	r.Check("the order failed rather than hanging", ok)
	r.Note("phase=%s failure=%s", st.Phase, truncate(st.Failure, 90))

	ledger, err := l.Ledger(id)
	r.Must(err, "ledger")
	res, err := l.Reservations(id)
	r.Must(err, "reservations")
	stock1, err := l.Stock()
	r.Must(err, "stock")

	r.Measure("charges / refunds on the statement", strconv.Itoa(ledger.Charges)+" / "+strconv.Itoa(ledger.Refunds))
	r.Measure("net taken from the customer (cents)", ledger.NetCents)
	r.Measure("compensations run, in order", strings.Join(st.Compensated, " -> "))

	r.Anomaly("the charge row is still there — nothing was undone", ledger.Charges == 1 && ledger.ChargedCents == amount)
	r.Anomaly("the customer's statement shows TWO entries for an order that never shipped",
		ledger.Charges == 1 && ledger.Refunds == 1)
	r.Fixed("the net position is zero", ledger.NetCents == 0)
	r.Fixed("the stock is back on the shelf", stock1["WIDGET-1"] == stock0["WIDGET-1"])
	r.Fixed("the reservation is released", len(res) == 1 && res[0].State == "RELEASED")
	r.Fixed("compensations ran in reverse: refund before release",
		len(st.Compensated) == 2 && st.Compensated[0] == "refund-payment" && st.Compensated[1] == "release-inventory")
	r.Note("reverse order is not a style choice: releasing stock before refunding would")
	r.Note("let a second order buy the item the first is still holding money for.")

	// --------------------------------------- the rollback that cannot finish
	r.Step("now break the refund too — a failure during the failure")
	r.Must(l.Reset(), "reset services")
	r.Must(l.Chaos("shipping", "before_commit", -1, 0), "arm shipping chaos")
	// target=refund, so the charge still works. Breaking the whole payments
	// service would fail the charge and never reach the interesting part.
	r.Must(l.ChaosTarget("payments", "before_commit", -1, 0, "refund"), "break refunds only")

	stuckID := lab.ID("stuck")
	_, err = l.CreateOrder(orders.CreateRequest{OrderID: stuckID, Customer: "dan", AmountCents: amount, Qty: 2})
	r.Must(err, "create order")

	st, ok = l.WaitForPhase(stuckID, 120*time.Second, saga.PhaseStuck, saga.PhaseFailed)
	r.Check("the order reached a terminal state", ok)

	ledger, err = l.Ledger(stuckID)
	r.Must(err, "ledger")
	res, err = l.Reservations(stuckID)
	r.Must(err, "reservations")

	r.Measure("net still taken from the customer (cents)", ledger.NetCents)
	r.Measure("phase", st.Phase)
	r.Measure("compensation errors", truncate(strings.Join(st.CompensationErrors, "; "), 96))

	r.Anomaly("the refund never happened: the customer is still out of pocket", ledger.NetCents == amount)
	r.Fixed("the order is marked STUCK, not FAILED — a human is required", st.Phase == saga.PhaseStuck)
	r.Fixed("the failing step is named in the result", len(st.CompensationErrors) == 1 &&
		strings.HasPrefix(st.CompensationErrors[0], "refund-payment"))
	r.Fixed("the rollback did NOT stop at the first failure: the stock still came back",
		len(res) == 1 && res[0].State == "RELEASED")
	r.Note("this is the part no orchestrator can fix for you. What it can do is refuse")
	r.Note("to report success, name the step, and keep the record where an alert can find it.")

	r.Must(l.Reset(), "reset services")
	r.Done()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
