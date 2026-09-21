package dedupe

import (
	"fmt"
	"path/filepath"
	"sync"
	"testing"
)

func TestFirstTimeIsTrueOnceThenFalse(t *testing.T) {
	g := New(16)
	if !g.FirstTime("a") {
		t.Fatal("first sight of a should be new")
	}
	if g.FirstTime("a") {
		t.Fatal("second sight of a should be a duplicate")
	}
	if !g.FirstTime("b") {
		t.Fatal("b is a different id and should be new")
	}
}

// The bounded window is a documented limitation, so pin it: an id that has
// been evicted looks new again. If you ever make this durable, this is the
// test that should start failing.
func TestForgetsBeyondItsWindow(t *testing.T) {
	g := New(4)
	g.FirstTime("old")
	for i := range 4 {
		g.FirstTime(fmt.Sprintf("filler-%d", i))
	}
	if !g.FirstTime("old") {
		t.Fatal("expected 'old' to have been evicted from a 4-entry window")
	}
}

func TestConcurrentCallersElectExactlyOneWinner(t *testing.T) {
	g := New(128)
	const racers = 50
	var wg sync.WaitGroup
	wins := make(chan struct{}, racers)
	for range racers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if g.FirstTime("same-id") {
				wins <- struct{}{}
			}
		}()
	}
	wg.Wait()
	close(wins)
	if n := len(wins); n != 1 {
		t.Fatalf("exactly one caller should win the right to apply the event, got %d", n)
	}
}

// The whole point of Open: the table must still know what it applied after
// the process that applied it has died. If this test fails, the guard cannot
// defend against the one failure mode it exists for.
func TestOpenRemembersAcrossRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "applied.log")

	g1, err := Open(path, 64)
	if err != nil {
		t.Fatal(err)
	}
	if !g1.FirstTime("evt-1") {
		t.Fatal("evt-1 should be new to a fresh table")
	}
	if err := g1.Close(); err != nil {
		t.Fatal(err)
	}

	// A new process, same durable table.
	g2, err := Open(path, 64)
	if err != nil {
		t.Fatal(err)
	}
	defer g2.Close()
	if g2.FirstTime("evt-1") {
		t.Fatal("evt-1 was applied before the restart and must not be applied again")
	}
	if !g2.FirstTime("evt-2") {
		t.Fatal("evt-2 is genuinely new and should be allowed through")
	}
}

func TestOpenOnMissingFileStartsEmpty(t *testing.T) {
	g, err := Open(filepath.Join(t.TempDir(), "nested", "applied.log"), 8)
	if err != nil {
		t.Fatalf("Open should create the directory and file: %v", err)
	}
	defer g.Close()
	if !g.FirstTime("anything") {
		t.Fatal("a fresh table should treat everything as new")
	}
}
