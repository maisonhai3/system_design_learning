// Command inventory runs the inventory service.
package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/inventory"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/store"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.Open(labhttp.Env("LAB_DB", "data/inventory.db"), inventory.DDL)
	if err != nil {
		log.Fatalf("inventory: open db: %v", err)
	}
	addr := ":" + labhttp.Env("LAB_PORT", "8112")
	log.Printf("inventory listening on %s", addr)
	if err := labhttp.Serve(ctx, addr, inventory.New(db).Routes()); err != nil {
		log.Fatalf("inventory: %v", err)
	}
}
