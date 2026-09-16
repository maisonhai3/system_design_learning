package main

import (
	"testing"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
)

func TestDecide(t *testing.T) {
	order := func(customer string, amount int) event.Event {
		ev := event.New(event.OrderPlaced, "o-1")
		ev.Customer, ev.Amount = customer, amount
		return ev
	}
	tests := []struct {
		name string
		ev   event.Event
		want bool
	}{
		{"ordinary order is approved", order("ada", 4000), true},
		{"exactly at the limit is approved", order("ada", limitCents), true},
		{"one cent over the limit is declined", order("ada", limitCents+1), false},
		{"broke customer is declined regardless of amount", order("broke", 1), false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, reason := decide(tt.ev)
			if got != tt.want {
				t.Fatalf("decide() = %v, want %v", got, tt.want)
			}
			if !got && reason == "" {
				t.Fatal("a decline must say why; a silent decline is unsupportable in production")
			}
		})
	}
}

// This is the property the idempotency guard exists to protect. It fails
// without a guard, which is the point of scenarios/04_duplicates.
func TestLedgerDoubleApplyChargesTwice(t *testing.T) {
	book := newLedger()
	if got := book.apply("o-1", 4000); got != 4000 {
		t.Fatalf("first charge total = %d, want 4000", got)
	}
	if got := book.apply("o-1", 4000); got != 8000 {
		t.Fatalf("second charge total = %d, want 8000 — applying an event twice moves money twice", got)
	}
}

func TestLedgerRefundReversesCharge(t *testing.T) {
	book := newLedger()
	book.apply("o-1", 30000)
	if got := book.apply("o-1", -30000); got != 0 {
		t.Fatalf("balance after refund = %d, want 0", got)
	}
}
