// Command payments runs the payments service.
package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/payments"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/store"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.Open(labhttp.Env("LAB_DB", "data/payments.db"), payments.DDL)
	if err != nil {
		log.Fatalf("payments: open db: %v", err)
	}
	addr := ":" + labhttp.Env("LAB_PORT", "8111")
	log.Printf("payments listening on %s", addr)
	if err := labhttp.Serve(ctx, addr, payments.New(db).Routes()); err != nil {
		log.Fatalf("payments: %v", err)
	}
}
