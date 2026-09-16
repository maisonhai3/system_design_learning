// Command gateway is the edge of the system: it turns HTTP requests into
// facts on a log, and then stops caring.
//
// This is the seam worth studying. The caller gets 202 Accepted as soon as the
// order is durably on the log — not when it has been paid for, not when stock
// has been reserved. In exchange for that fast, always-available write, the
// caller gives up the right to a synchronous answer and has to go look the
// order up later (that is what the projector is for).
//
// That trade is the whole of event-driven design, and it is a genuine trade.
// If your product needs to tell the user "declined" in the same HTTP response,
// no amount of Kafka will give you that, and you should make the call
// synchronously instead.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/console"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/kafkaio"
)

// catalog is the gateway's private data. Inventory has its own idea of what
// exists, and the two are allowed to disagree — that is not a bug to fix, it
// is what "each service owns its data" actually feels like in practice.
var catalog = map[string]int{
	"bolt":      100,   // $1.00   — deep stock, for load scenarios
	"widget":    2000,  // $20.00
	"gizmo":     15000, // $150.00
	"doohickey": 30000, // $300.00
}

// price is the only rule this service has, so it is the only thing worth
// testing here.
func price(item string, qty int) (int, error) {
	unit, ok := catalog[item]
	if !ok {
		return 0, fmt.Errorf("unknown item %q (have: %s)", item, strings.Join(items(), ", "))
	}
	if qty < 1 || qty > 100 {
		return 0, fmt.Errorf("qty must be between 1 and 100, got %d", qty)
	}
	return unit * qty, nil
}

func items() []string {
	out := make([]string, 0, len(catalog))
	for k := range catalog {
		out = append(out, k)
	}
	return out
}

type orderRequest struct {
	Customer string `json:"customer"`
	Item     string `json:"item"`
	Qty      int    `json:"qty"`
}

func main() {
	brokers := flag.String("brokers", "localhost:9092", "comma-separated Kafka brokers")
	addr := flag.String("addr", ":8080", "HTTP listen address")
	flag.Parse()

	log := console.New("gateway")
	prod := kafkaio.NewProducer(strings.Split(*brokers, ","), log)
	defer prod.Close()

	var seq atomic.Int64

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintln(w, "ok")
	})
	mux.HandleFunc("POST /orders", func(w http.ResponseWriter, r *http.Request) {
		var req orderRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad JSON: "+err.Error(), http.StatusBadRequest)
			return
		}
		amount, err := price(req.Item, req.Qty)
		if err != nil {
			// Reject what we can judge locally and cheaply. Everything we
			// cannot judge here (can they pay? is there stock?) goes on the
			// log and is somebody else's decision.
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if req.Customer == "" {
			http.Error(w, "customer is required", http.StatusBadRequest)
			return
		}

		ev := event.New(event.OrderPlaced, fmt.Sprintf("o-%d", seq.Add(1)))
		ev.Customer, ev.Item, ev.Qty, ev.Amount = req.Customer, req.Item, req.Qty, amount

		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		if err := prod.Publish(ctx, event.TopicOrders, ev); err != nil {
			// The write did not make it to the log, so we must NOT tell the
			// caller we accepted it. A 202 we cannot back up is how orders
			// vanish.
			http.Error(w, "could not accept order: "+err.Error(), http.StatusServiceUnavailable)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"order_id": ev.OrderID,
			"amount":   event.USD(ev.Amount),
			"status":   "accepted",
			"hint":     "the order is on the log; ask the projector on :8081 what became of it",
		})
	})

	srv := &http.Server{Addr: *addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()

	log.Say("listening on %s — POST /orders {customer,item,qty}", *addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Warn("server stopped: %v", err)
		os.Exit(1)
	}
	log.Say("bye")
}
