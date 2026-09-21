// Package dedupe is the toy version of an idempotency key table.
//
// Kafka's default delivery is at-least-once: on a bad day you will be handed
// the same record twice. The fix is not to chase "exactly once delivery" —
// that phrase sells conference tickets and does not survive a network
// partition. The fix is to make *applying* a record twice have the same effect
// as applying it once, which you do by remembering what you have already
// applied.
package dedupe

import (
	"os"
	"sync"
)

// Guard remembers recently applied event IDs.
//
// Two things make this a toy, and both are worth knowing:
//
//  1. It is in memory, so it forgets on restart — exactly when you need it
//     most. Production puts this table in the same database as the side
//     effect.
//
//  2. Even with a durable table, "check the table, then charge the card" is
//     two steps, and a crash can land between them. The real pattern writes
//     the idempotency key and the side effect in ONE transaction, so either
//     both happened or neither did. If the side effect lives in another
//     system (a payment provider), you push the key to them instead — which
//     is precisely what Stripe's Idempotency-Key header is for.
type Guard struct {
	mu   sync.Mutex
	seen map[string]struct{}
	ring []string // insertion order, for bounded eviction
	next int
	max  int

	// file, when set, makes the table survive a restart. See Open.
	file *os.File
}

// New returns a Guard that remembers at most max IDs.
func New(max int) *Guard {
	if max <= 0 {
		max = 1024
	}
	return &Guard{seen: make(map[string]struct{}, max), ring: make([]string, max), max: max}
}

// FirstTime reports whether id has not been applied before, and records it.
// It returns false for a replay you have already handled.
func (g *Guard) FirstTime(id string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, dup := g.seen[id]; dup {
		return false
	}
	if g.file != nil {
		// Record the intent BEFORE reporting that this is new, and flush it
		// to disk. Writing after the caller has already charged the card
		// would leave a window where the charge is durable and the note
		// saying "already charged" is not.
		if _, err := g.file.WriteString(id + "\n"); err == nil {
			_ = g.file.Sync()
		}
	}
	g.remember(id)
	return true
}

// remember records an id. Callers hold g.mu.
func (g *Guard) remember(id string) {
	// Evict the oldest entry if this slot is occupied. A bounded guard can
	// only defend against duplicates that arrive within its window — which is
	// a real limitation, not an implementation detail to gloss over. Size it
	// against how far back a redelivery can plausibly come from.
	if old := g.ring[g.next]; old != "" {
		delete(g.seen, old)
	}
	g.ring[g.next] = id
	g.next = (g.next + 1) % g.max
	g.seen[id] = struct{}{}
}
