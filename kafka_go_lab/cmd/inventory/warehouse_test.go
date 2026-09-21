package main

import (
	"sync"
	"testing"
)

func TestReserve(t *testing.T) {
	w := newWarehouse()

	ok, left, _ := w.reserve("widget", 3)
	if !ok || left != 7 {
		t.Fatalf("reserve(widget,3) = %v, left %d; want true, 7", ok, left)
	}

	// The whole point of doohickey: paid for, then unavailable.
	if ok, _, reason := w.reserve("doohickey", 1); ok || reason == "" {
		t.Fatalf("doohickey is out of stock and must be rejected with a reason, got ok=%v reason=%q", ok, reason)
	}

	if ok, _, _ := w.reserve("sprocket", 1); ok {
		t.Fatal("unknown item must not be reservable")
	}

	// Partial fills are refused outright: 5 gizmos exist, asking for 6 gets
	// you nothing rather than 5. Silently shipping less than ordered is a
	// business decision, not a default.
	if ok, left, _ := w.reserve("gizmo", 6); ok || left != 5 {
		t.Fatalf("over-request should reserve nothing, got ok=%v left=%d", ok, left)
	}
}

// Two concurrent reservations must not oversell the last unit.
func TestReserveDoesNotOversell(t *testing.T) {
	w := &warehouse{qty: map[string]int{"widget": 1}}
	var wg sync.WaitGroup
	wins := make(chan struct{}, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ok, _, _ := w.reserve("widget", 1); ok {
				wins <- struct{}{}
			}
		}()
	}
	wg.Wait()
	close(wins)
	if n := len(wins); n != 1 {
		t.Fatalf("exactly one reservation should win the last widget, got %d", n)
	}
	if w.qty["widget"] != 0 {
		t.Fatalf("shelf should be empty, got %d", w.qty["widget"])
	}
}
