// Package saga holds the order-fulfilment process: the types, the activities
// that call the three microservices, and the Temporal workflow that sequences
// them.
//
// The same process is implemented a second time, without Temporal, in
// internal/naive. Keeping them side by side is the whole design of this lab —
// you cannot see what durable execution is worth by reading one of them.
package saga

const (
	// TaskQueue is the name workers poll and clients target. It is not a
	// topic: a workflow task goes to exactly one worker, and which one is
	// nobody's business. Deploying a new worker is how you deploy new code.
	TaskQueue = "orders"

	// SignalApproval is the name the outside world uses to talk to a running
	// workflow. A signal is durable: it is written to history before it is
	// delivered, so it cannot be lost by a worker dying between the two.
	SignalApproval = "approval"

	// QueryState reads a running workflow's state without touching a database.
	// For in-flight work the workflow IS the read model.
	QueryState = "state"

	// ApprovalThresholdCents is the business rule that puts a human in the
	// loop. Above this, someone has to say yes.
	ApprovalThresholdCents = 50000 // $500.00
)

// Phase is the coarse state of an order, and the thing a query returns.
type Phase string

const (
	PhaseReceived         Phase = "RECEIVED"
	PhaseAwaitingApproval Phase = "AWAITING_APPROVAL"
	PhaseReserved         Phase = "RESERVED"
	PhaseCharged          Phase = "CHARGED"
	PhasePacking          Phase = "PACKING"
	PhaseShipped          Phase = "SHIPPED"
	PhaseCompleted        Phase = "COMPLETED"
	PhaseCompensating     Phase = "COMPENSATING"
	PhaseFailed           Phase = "FAILED"   // failed, and rolled back cleanly
	PhaseStuck            Phase = "STUCK"    // failed, and the rollback did not finish
	PhaseRejected         Phase = "REJECTED" // a human said no, or never said anything
)

// OrderInput is what starts the process. It is a workflow argument, which means
// it is serialised into history and replayed forever — so it must stay
// backwards-compatible in exactly the way any wire format must.
type OrderInput struct {
	OrderID     string `json:"order_id"`
	Customer    string `json:"customer"`
	SKU         string `json:"sku"`
	Qty         int    `json:"qty"`
	AmountCents int64  `json:"amount_cents"`
	Address     string `json:"address"`

	// ApprovalTimeoutSeconds is how long a human has to approve a large order.
	// In production this is days. In a scenario it is seconds. The workflow
	// code does not change between those two, which is the point: a timer is
	// a row on a server, not a goroutine you are paying to keep alive.
	ApprovalTimeoutSeconds int `json:"approval_timeout_seconds,omitempty"`

	// PackSeconds is a deliberate pause between charging and shipping, so a
	// scenario has a window in which to kill the process mid-saga.
	PackSeconds int `json:"pack_seconds,omitempty"`

	// IdempotencyMode picks how the activities treat the key this workflow
	// hands them: "stable" (the default and the only correct one),
	// "per-attempt", or "off". It is per-order so scenario 02 can run the safe
	// and unsafe versions against the same worker, seconds apart, and compare
	// two ledgers rather than two anecdotes.
	IdempotencyMode KeyMode `json:"idempotency_mode,omitempty"`
}

// OrderState is what a query returns and what the workflow finishes with.
type OrderState struct {
	OrderID       string   `json:"order_id"`
	Phase         Phase    `json:"phase"`
	ReservationID string   `json:"reservation_id,omitempty"`
	ChargeID      string   `json:"charge_id,omitempty"`
	ShipmentID    string   `json:"shipment_id,omitempty"`
	Approved      *bool    `json:"approved,omitempty"`
	Failure       string   `json:"failure,omitempty"`
	Compensated   []string `json:"compensated,omitempty"`
	// CompensationErrors is the field nobody wants to need. A saga that cannot
	// finish rolling back has left the business in a state no code will fix,
	// and the only correct behaviour is to say so loudly.
	CompensationErrors []string `json:"compensation_errors,omitempty"`
}

// ApprovalSignal is the payload sent to a waiting workflow.
type ApprovalSignal struct {
	Approved bool   `json:"approved"`
	By       string `json:"by,omitempty"`
}
