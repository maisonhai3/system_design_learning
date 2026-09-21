// Package store is each service's own database.
//
// It is SQLite, one file per service, and that is the point. "Database per
// service" is the rule everyone repeats and then quietly breaks the first time
// a join would be convenient. Here it is enforced by physics: payments cannot
// read inventory's table because it is a different file, in a different
// container, that payments has never opened.
//
// Everything this lab teaches about sagas follows from that one constraint. If
// the three services shared a database you would use a transaction, not a saga,
// and you would not need any of this.
package store

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	_ "modernc.org/sqlite" // pure-Go driver: no cgo, so the image stays FROM scratch-ish
)

type DB struct{ *sql.DB }

func Open(path, ddl string) (*DB, error) {
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	// WAL so a reader (GET /ledger) never blocks the writer, busy_timeout so a
	// brief overlap waits instead of erroring.
	dsn := fmt.Sprintf("file:%s?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)", path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// One writer. SQLite allows exactly one anyway; making it explicit turns a
	// confusing "database is locked" into a short queue. A real service would
	// use a server database and a pool — but then the lab would need a fourth
	// container to teach something it is not about.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(ddl); err != nil {
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return &DB{db}, nil
}

// IsUnique reports whether err is a uniqueness violation.
//
// This is the load-bearing line of the whole idempotency story: the second
// charge attempt does not get a polite "already exists" from the application,
// it gets rejected by an index. The check is a string match because SQLite's
// pure-Go driver does not export its error codes; a production service would
// match on the code, and would still be relying on the constraint rather than
// on a SELECT-then-INSERT, which is a race, not a check.
func IsUnique(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	return strings.Contains(s, "UNIQUE constraint failed") || strings.Contains(s, "constraint failed: UNIQUE")
}
