// Scenario 02 — the customer gets charged twice, and the order says it worked.
package main

import (
	"time"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/lab"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

const amount = 4200

func main() {
	r := lab.NewRun("02 — at-least-once is the deal, and you pay the difference",
		"Temporal guarantees an activity runs AT LEAST once. When the payment service commits and then "+
			"fails to answer, the retry is a second charge — unless the key it sends is the same one it "+
			"sent last time. Retries do not cause this bug; they make it certain.")
	l := lab.Open()
	r.Must(l.Reset(), "reset services")

	// ------------------------------------------------- the bug, per-attempt key
	r.Step("arm payments: commit the charge, THEN fail the response (once)")
	r.Must(l.Chaos("payments", "after_commit", 1, 0), "arm chaos")

	bugID := lab.ID("key_per_attempt")
	_, err := l.CreateOrder(orders.CreateRequest{
		OrderID: bugID, Customer: "bob", AmountCents: amount,
		IdempotencyMode: "per-attempt", // uuid.New() inside the activity
	})
	r.Must(err, "create order")

	st, ok := l.WaitForPhase(bugID, 60*time.Second, saga.PhaseCompleted, saga.PhaseFailed, saga.PhaseStuck)
	r.Check("the order reached a terminal state", ok)

	bug, err := l.Ledger(bugID)
	r.Must(err, "ledger")
	r.Measure("charges on the customer's card", bug.Charges)
	r.Measure("amount taken (cents)", bug.ChargedCents)
	r.Measure("amount the order was for (cents)", amount)

	r.Anomaly("the customer was charged twice", bug.Charges == 2)
	r.Anomaly("twice the money left their account", bug.ChargedCents == 2*amount)
	r.Anomaly("...and the order reports success, so nothing alerts", st.Phase == saga.PhaseCompleted)
	r.Note("the retry was correct. The service was correct. The key was new.")

	// -------------------------------------------------------- the fix, stable key
	r.Step("same fault, same retry — with the key computed in workflow code")
	r.Must(l.Chaos("payments", "after_commit", 1, 0), "arm chaos")

	fixID := lab.ID("key_stable")
	_, err = l.CreateOrder(orders.CreateRequest{
		OrderID: fixID, Customer: "bob", AmountCents: amount,
		IdempotencyMode: "stable", // orderID + "/charge", identical on every attempt
	})
	r.Must(err, "create order")

	st, ok = l.WaitForPhase(fixID, 60*time.Second, saga.PhaseCompleted, saga.PhaseFailed, saga.PhaseStuck)
	r.Check("the order reached a terminal state", ok)

	fix, err := l.Ledger(fixID)
	r.Must(err, "ledger")
	r.Measure("charges on the customer's card", fix.Charges)
	r.Measure("amount taken (cents)", fix.ChargedCents)

	r.Fixed("charged exactly once despite the retry", fix.Charges == 1)
	r.Fixed("the customer paid exactly what the order was for", fix.ChargedCents == amount)
	r.Fixed("the order still completed", st.Phase == saga.PhaseCompleted)
	r.Note("the second attempt hit a UNIQUE index and was handed back the first charge")

	// ------------------------------------------------------ no key at all
	r.Step("and with no key at all, for completeness")
	r.Must(l.Chaos("payments", "after_commit", 1, 0), "arm chaos")

	offID := lab.ID("key_off")
	_, err = l.CreateOrder(orders.CreateRequest{
		OrderID: offID, Customer: "bob", AmountCents: amount, IdempotencyMode: "off",
	})
	r.Must(err, "create order")
	_, _ = l.WaitForPhase(offID, 60*time.Second, saga.PhaseCompleted, saga.PhaseFailed, saga.PhaseStuck)
	off, err := l.Ledger(offID)
	r.Must(err, "ledger")
	r.Anomaly("no key means no protection: charged twice again", off.Charges == 2)
	r.Note("NULL is distinct from NULL in a UNIQUE index — in SQLite, in Postgres, in the standard.")
	r.Note("A nullable key column protects exactly the rows that bothered to fill it in.")

	// --------------------------------------------- the free one: workflow id
	r.Step("the duplicate nobody writes code for: the same order submitted twice")
	r.Must(l.ClearChaos("payments"), "clear chaos")

	dupID := lab.ID("double_submit")
	first, err := l.CreateOrder(orders.CreateRequest{OrderID: dupID, Customer: "carol", AmountCents: amount})
	r.Must(err, "first submit")
	second, err := l.CreateOrder(orders.CreateRequest{OrderID: dupID, Customer: "carol", AmountCents: amount})
	r.Must(err, "second submit")

	_, _ = l.WaitForPhase(dupID, 60*time.Second, saga.PhaseCompleted, saga.PhaseFailed)
	dup, err := l.Ledger(dupID)
	r.Must(err, "ledger")

	r.Fixed("the second submit was rejected as a duplicate", second.Duplicate && !first.Duplicate)
	r.Fixed("one saga ran, one charge was made", dup.Charges == 1)
	r.Note("no dedupe table, no request cache: the order id is the workflow id, and")
	r.Note("the server will not run two executions under one id. Naming is a feature.")

	r.Done()
}
