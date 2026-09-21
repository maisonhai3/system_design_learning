// Command shipping runs the shipping service.
package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/shipping"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/store"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.Open(labhttp.Env("LAB_DB", "data/shipping.db"), shipping.DDL)
	if err != nil {
		log.Fatalf("shipping: open db: %v", err)
	}
	addr := ":" + labhttp.Env("LAB_PORT", "8113")
	log.Printf("shipping listening on %s", addr)
	if err := labhttp.Serve(ctx, addr, shipping.New(db).Routes()); err != nil {
		log.Fatalf("shipping: %v", err)
	}
}
