package dedupe

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Open returns a Guard whose memory survives the process.
//
// This exists because of a trap worth walking into once: an in-memory
// idempotency table is useless against exactly the failure it is meant to
// cover. The record gets redelivered *because* the process died, and the
// process dying is what emptied the table. A guard that forgets at restart
// protects you from everything except the thing that actually happens.
//
// So the table has to be as durable as the side effect it guards. Here that
// means an append-only file and an fsync per entry; in a real service it means
// a row in the same database — and ideally in the same transaction as the side
// effect, so the two cannot disagree.
func Open(path string, max int) (*Guard, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	g := New(max)

	// Replay what we already applied before we died.
	if f, err := os.Open(path); err == nil {
		s := bufio.NewScanner(f)
		for s.Scan() {
			if id := strings.TrimSpace(s.Text()); id != "" {
				g.mu.Lock()
				g.remember(id)
				g.mu.Unlock()
			}
		}
		err := s.Err()
		f.Close()
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}

	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	g.file = f
	return g, nil
}

// Close releases the backing file, if there is one.
func (g *Guard) Close() error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.file == nil {
		return nil
	}
	err := g.file.Close()
	g.file = nil
	return err
}
