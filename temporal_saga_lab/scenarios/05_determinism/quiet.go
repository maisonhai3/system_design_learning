package main

import (
	"io"
	"log/slog"
)

// The replayer logs the non-determinism as a panic, with a full SDK stack
// trace, before returning it as an error. That output is expected here — it is
// the thing the scenario is demonstrating — and printing it in the middle of
// the findings makes them unreadable. The scenario reports the error itself, so
// the logger is discarded.
func newQuietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}
