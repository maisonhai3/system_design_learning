package main

import (
	"testing"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
)

// fold replays a sequence of event types through apply, the way a real replay
// from offset 0 would.
func fold(types ...string) view {
	var v view
	for _, typ := range types {
		ev := event.New(typ, "o-1")
		ev.Item, ev.Qty, ev.Amount, ev.Customer = "widget", 2, 4000, "ada"
		if typ == event.PaymentFailed || typ == event.StockRejected {
			ev.Reason = "because"
		}
		v = apply(v, ev)
	}
	return v
}

func TestApplyHappyPath(t *testing.T) {
	v := fold(event.OrderPlaced, event.PaymentCompleted, event.StockReserved)
	if v.Status != "confirmed" {
		t.Fatalf("status = %q, want confirmed", v.Status)
	}
	if v.Events != 3 || v.Amount != "$40.00" || v.Customer != "ada" {
		t.Fatalf("view did not accumulate the order details: %+v", v)
	}
}

func TestApplyPaymentDeclined(t *testing.T) {
	v := fold(event.OrderPlaced, event.PaymentFailed)
	if v.Status != "cancelled" || v.Reason == "" {
		t.Fatalf("declined order = %+v, want cancelled with a reason", v)
	}
}

func TestApplyStockRejectedThenRefunded(t *testing.T) {
	v := fold(event.OrderPlaced, event.PaymentCompleted, event.StockRejected, event.PaymentRefunded)
	if v.Status != "cancelled" {
		t.Fatalf("status = %q, want cancelled", v.Status)
	}
	if !v.Refunded {
		t.Fatal("a refund must be visible in the read model, or support cannot answer 'where is my money'")
	}
}

// A projector is a fold over a log, so replaying the same log must land on the
// same state. If this ever fails, the projector has smuggled in state that is
// not derived from events, and rebuilding it will silently produce a different
// answer than the one you have in production.
func TestApplyIsDeterministic(t *testing.T) {
	seq := []string{event.OrderPlaced, event.PaymentCompleted, event.StockReserved}
	a, b := fold(seq...), fold(seq...)
	if a.Status != b.Status || a.Events != b.Events || a.Amount != b.Amount {
		t.Fatalf("replaying the same events gave different results:\n %+v\n %+v", a, b)
	}
}

// The important one.
//
// The projector reads three topics, and Kafka orders records within a
// partition only — never across topics. A replay therefore hands these events
// over in a different order than they happened, so every permutation must fold
// to the same answer. This test fails against the obvious "last event wins"
// implementation, which is exactly why it is here.
func TestApplyIsOrderIndependent(t *testing.T) {
	cases := [][]string{
		{event.OrderPlaced, event.PaymentCompleted, event.StockReserved},
		{event.OrderPlaced, event.PaymentCompleted, event.StockRejected, event.PaymentRefunded},
		{event.OrderPlaced, event.PaymentFailed},
	}
	for _, seq := range cases {
		want := fold(seq...)
		for _, perm := range permutations(seq) {
			got := fold(perm...)
			if got.Status != want.Status || got.Refunded != want.Refunded {
				t.Fatalf("order %v folded to %q (refunded=%v); order %v folded to %q (refunded=%v)\n"+
					"a projection must be a function of the SET of events, not the order they arrive in",
					seq, want.Status, want.Refunded, perm, got.Status, got.Refunded)
			}
		}
	}
}

func permutations(in []string) [][]string {
	if len(in) <= 1 {
		return [][]string{append([]string(nil), in...)}
	}
	var out [][]string
	for i := range in {
		rest := make([]string, 0, len(in)-1)
		rest = append(rest, in[:i]...)
		rest = append(rest, in[i+1:]...)
		for _, p := range permutations(rest) {
			out = append(out, append([]string{in[i]}, p...))
		}
	}
	return out
}

// Consumers must tolerate event types they were not taught about, or every
// producer change becomes a coordinated deploy.
func TestApplyIgnoresUnknownEventTypes(t *testing.T) {
	v := fold(event.OrderPlaced)
	after := apply(v, event.New("SomethingInventedNextQuarter", "o-1"))
	if after.Status != "placed" {
		t.Fatalf("unknown event changed status to %q; it should have been ignored", after.Status)
	}
	if after.Events != v.Events+1 {
		t.Fatal("the event should still be counted as seen")
	}
}

func TestNaturalOrderIsNumericNotLexicographic(t *testing.T) {
	if !(natural("o-2") < natural("o-10")) {
		t.Fatal("o-2 must sort before o-10")
	}
}
