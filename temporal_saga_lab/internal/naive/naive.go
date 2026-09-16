// Package naive is the same saga, orchestrated the way most services do it:
// a goroutine, a map, and a retry loop.
//
// Read it before you read the workflow. It is not a strawman — it retries with
// backoff, it distinguishes retryable from permanent failures, it compensates
// in reverse order, and it supports the human approval gate. Someone competent
// wrote it. It is about 180 lines, against the workflow's 180, and for the
// happy path and the ordinary unhappy paths it does the same job.
//
// It has exactly one thing wrong with it: every piece of state that says how
// far this order got lives in this process's memory. Kill the process and the
// order does not fail — it stops existing, halfway through, with the customer's
// money already taken. No retry fires because the thing that would have retried
// is the thing that died.
//
// That is the whole argument for durable execution, and it costs a paragraph to
// state and a career to internalise.
package naive

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/inventory"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/payments"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/shipping"
)

type Orchestrator struct {
	Client       *labhttp.Client
	PaymentsURL  string
	InventoryURL string
	ShippingURL  string

	// The entire durability story of this implementation. A map. When this
	// process exits, every in-flight order goes with it — and unlike a dropped
	// request, nobody gets an error, because there is nobody left to tell.
	mu       sync.Mutex
	orders   map[string]*saga.OrderState
	approval map[string]chan saga.ApprovalSignal
}

func New(client *labhttp.Client, paymentsURL, inventoryURL, shippingURL string) *Orchestrator {
	return &Orchestrator{
		Client: client, PaymentsURL: paymentsURL, InventoryURL: inventoryURL, ShippingURL: shippingURL,
		orders: map[string]*saga.OrderState{}, approval: map[string]chan saga.ApprovalSignal{},
	}
}

func (o *Orchestrator) Get(orderID string) (saga.OrderState, bool) {
	o.mu.Lock()
	defer o.mu.Unlock()
	st, ok := o.orders[orderID]
	if !ok {
		return saga.OrderState{}, false
	}
	return *st, true
}

func (o *Orchestrator) Approve(orderID string, sig saga.ApprovalSignal) error {
	o.mu.Lock()
	ch, ok := o.approval[orderID]
	o.mu.Unlock()
	if !ok {
		// After a restart this is what an approval looks like: a 404 for an
		// order the customer can see in their email. The signal has nowhere to
		// go because the waiting thing was a goroutine.
		return errors.New("no order is waiting for approval with that id (this process may have restarted)")
	}
	select {
	case ch <- sig:
		return nil
	default:
		return errors.New("approval channel is not being read")
	}
}

func (o *Orchestrator) set(orderID string, mutate func(*saga.OrderState)) {
	o.mu.Lock()
	defer o.mu.Unlock()
	st, ok := o.orders[orderID]
	if !ok {
		st = &saga.OrderState{OrderID: orderID}
		o.orders[orderID] = st
	}
	mutate(st)
}

// Start runs the saga in a goroutine, which is the natural thing to write and
// the exact reason this design cannot survive a deploy.
func (o *Orchestrator) Start(in saga.OrderInput) {
	o.set(in.OrderID, func(s *saga.OrderState) { s.Phase = saga.PhaseReceived })
	o.mu.Lock()
	o.approval[in.OrderID] = make(chan saga.ApprovalSignal, 1)
	o.mu.Unlock()
	go o.run(in)
}

func (o *Orchestrator) run(in saga.OrderInput) {
	ctx := context.Background()
	var undo []func() error
	var undoNames []string
	push := func(name string, fn func() error) {
		undoNames = append(undoNames, name)
		undo = append(undo, fn)
	}

	compensate := func(cause error) {
		o.set(in.OrderID, func(s *saga.OrderState) {
			s.Phase = saga.PhaseCompensating
			s.Failure = cause.Error()
		})
		for i := len(undo) - 1; i >= 0; i-- {
			name := undoNames[i]
			if err := undo[i](); err != nil {
				o.set(in.OrderID, func(s *saga.OrderState) {
					s.CompensationErrors = append(s.CompensationErrors, fmt.Sprintf("%s: %v", name, err))
				})
				continue
			}
			o.set(in.OrderID, func(s *saga.OrderState) { s.Compensated = append(s.Compensated, name) })
		}
		o.set(in.OrderID, func(s *saga.OrderState) {
			if len(s.CompensationErrors) > 0 {
				s.Phase = saga.PhaseStuck
			} else {
				s.Phase = saga.PhaseFailed
			}
		})
	}

	// ---- approval gate: a select on a channel, and a timer that is a goroutine
	if in.AmountCents > saga.ApprovalThresholdCents {
		o.set(in.OrderID, func(s *saga.OrderState) { s.Phase = saga.PhaseAwaitingApproval })
		timeout := time.Duration(in.ApprovalTimeoutSeconds) * time.Second
		if in.ApprovalTimeoutSeconds == 0 {
			timeout = 72 * time.Hour // a 72-hour goroutine. Deploy weekly and it never fires.
		}
		o.mu.Lock()
		ch := o.approval[in.OrderID]
		o.mu.Unlock()
		select {
		case sig := <-ch:
			if !sig.Approved {
				o.set(in.OrderID, func(s *saga.OrderState) {
					s.Phase, s.Failure = saga.PhaseRejected, "rejected by reviewer"
				})
				return
			}
			approved := true
			o.set(in.OrderID, func(s *saga.OrderState) { s.Approved = &approved })
		case <-time.After(timeout):
			o.set(in.OrderID, func(s *saga.OrderState) {
				s.Phase, s.Failure = saga.PhaseRejected, "nobody approved in time"
			})
			return
		}
	}

	// ---- reserve
	var res inventory.Reservation
	err := o.retry(ctx, func(ctx context.Context) error {
		return o.Client.Post(ctx, o.InventoryURL+"/reservations", inventory.ReserveRequest{
			OrderID: in.OrderID, SKU: in.SKU, Qty: in.Qty,
			// A fresh key per attempt. It is the obvious thing to write: you
			// need a unique id, so you make one where you need it. It is also
			// worthless, because the whole job of the key is to be the SAME on
			// the attempt that repeats work the previous attempt already did.
			IdempotencyKey: uuid.NewString(),
		}, &res)
	})
	if err != nil {
		o.set(in.OrderID, func(s *saga.OrderState) { s.Phase, s.Failure = saga.PhaseFailed, err.Error() })
		return
	}
	o.set(in.OrderID, func(s *saga.OrderState) { s.ReservationID, s.Phase = res.ID, saga.PhaseReserved })
	push("release-inventory", func() error {
		return o.Client.Post(context.Background(), o.InventoryURL+"/reservations/"+res.ID+"/release", nil, nil)
	})

	// ---- charge
	var ch payments.Charge
	err = o.retry(ctx, func(ctx context.Context) error {
		return o.Client.Post(ctx, o.PaymentsURL+"/charges", payments.ChargeRequest{
			OrderID: in.OrderID, AmountCents: in.AmountCents, IdempotencyKey: uuid.NewString(),
		}, &ch)
	})
	if err != nil {
		compensate(err)
		return
	}
	o.set(in.OrderID, func(s *saga.OrderState) { s.ChargeID, s.Phase = ch.ID, saga.PhaseCharged })
	push("refund-payment", func() error {
		return o.Client.Post(context.Background(), o.PaymentsURL+"/refunds", payments.RefundRequest{
			ChargeID: ch.ID, OrderID: in.OrderID, IdempotencyKey: uuid.NewString(),
		}, nil)
	})

	// ---- pack
	//
	// time.Sleep. Identical in shape to the workflow's workflow.Sleep and
	// opposite in every property that matters: it holds a goroutine, it is
	// invisible to anything outside this process, and it ends when the process
	// does.
	if in.PackSeconds > 0 {
		o.set(in.OrderID, func(s *saga.OrderState) { s.Phase = saga.PhasePacking })
		time.Sleep(time.Duration(in.PackSeconds) * time.Second)
	}

	// ---- ship
	var sh shipping.Shipment
	err = o.retry(ctx, func(ctx context.Context) error {
		return o.Client.Post(ctx, o.ShippingURL+"/shipments", shipping.ShipRequest{
			OrderID: in.OrderID, Address: in.Address, IdempotencyKey: uuid.NewString(),
		}, &sh)
	})
	if err != nil {
		compensate(err)
		return
	}
	o.set(in.OrderID, func(s *saga.OrderState) {
		s.ShipmentID, s.Phase = sh.ID, saga.PhaseCompleted
	})
}

// retry is the hand-rolled version of the workflow's RetryPolicy struct. Same
// backoff, same attempt cap, same respect for non-retryable errors — and it
// lives in the orchestrator's memory, so a restart resets it to nothing.
func (o *Orchestrator) retry(ctx context.Context, fn func(context.Context) error) error {
	backoff := 200 * time.Millisecond
	var last error
	for attempt := 1; attempt <= 4; attempt++ {
		last = fn(ctx)
		if last == nil {
			return nil
		}
		var he *labhttp.Error
		if errors.As(last, &he) && !he.Retryable() {
			return last
		}
		if attempt < 4 {
			time.Sleep(backoff)
			if backoff *= 2; backoff > 2*time.Second {
				backoff = 2 * time.Second
			}
		}
	}
	return last
}
