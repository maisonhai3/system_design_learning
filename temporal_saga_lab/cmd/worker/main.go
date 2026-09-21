// Command worker hosts the workflow and the activities.
//
// This is the deployable unit that Temporal actually cares about. It holds no
// state — the server does — so you can run one, or twenty, or zero for a while.
// Running zero does not fail any orders; it pauses them.
package main

import (
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

func main() {
	hostPort := labhttp.Env("LAB_TEMPORAL", "localhost:7233")

	// The server may still be booting when this container starts. Retrying the
	// dial here rather than crash-looping keeps `up` logs readable.
	var c client.Client
	var err error
	for attempt := 1; attempt <= 30; attempt++ {
		c, err = client.Dial(client.Options{HostPort: hostPort})
		if err == nil {
			break
		}
		log.Printf("worker: waiting for temporal at %s (%v)", hostPort, err)
		time.Sleep(time.Second)
	}
	if err != nil {
		log.Fatalf("worker: cannot reach temporal at %s: %v", hostPort, err)
	}
	defer c.Close()

	acts := &saga.Activities{
		Client:       labhttp.NewClient(5 * time.Second),
		PaymentsURL:  labhttp.Env("LAB_PAYMENTS_URL", "http://localhost:8111"),
		InventoryURL: labhttp.Env("LAB_INVENTORY_URL", "http://localhost:8112"),
		ShippingURL:  labhttp.Env("LAB_SHIPPING_URL", "http://localhost:8113"),
		KeyMode:      saga.KeyModeFromEnv(),
	}

	w := worker.New(c, saga.TaskQueue, worker.Options{})
	w.RegisterWorkflow(saga.OrderWorkflow)
	w.RegisterActivity(acts)

	log.Printf("worker polling task queue %q  (idempotency=%s)", saga.TaskQueue, acts.KeyMode)

	// Start, then wait for a signal, rather than w.Run(nil): we want the
	// scenarios to be able to kill this process abruptly and see what happens.
	if err := w.Start(); err != nil {
		log.Fatalf("worker: %v", err)
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Printf("worker stopping")
	w.Stop()
}
