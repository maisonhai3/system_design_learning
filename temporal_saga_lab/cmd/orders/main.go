// Command orders runs the front door: the HTTP API that starts, queries and
// signals orders, in either orchestration mode.
package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"
	"time"

	"go.temporal.io/sdk/client"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/naive"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	hostPort := labhttp.Env("LAB_TEMPORAL", "localhost:7233")
	var c client.Client
	var err error
	for attempt := 1; attempt <= 30; attempt++ {
		c, err = client.Dial(client.Options{HostPort: hostPort})
		if err == nil {
			break
		}
		log.Printf("orders: waiting for temporal at %s", hostPort)
		time.Sleep(time.Second)
	}
	if err != nil {
		log.Fatalf("orders: cannot reach temporal at %s: %v", hostPort, err)
	}
	defer c.Close()

	httpClient := labhttp.NewClient(5 * time.Second)
	svc := &orders.Service{
		Temporal: c,
		Naive: naive.New(httpClient,
			labhttp.Env("LAB_PAYMENTS_URL", "http://localhost:8111"),
			labhttp.Env("LAB_INVENTORY_URL", "http://localhost:8112"),
			labhttp.Env("LAB_SHIPPING_URL", "http://localhost:8113")),
	}

	addr := ":" + labhttp.Env("LAB_PORT", "8110")
	log.Printf("orders listening on %s", addr)
	if err := labhttp.Serve(ctx, addr, svc.Routes()); err != nil {
		log.Fatalf("orders: %v", err)
	}
}
