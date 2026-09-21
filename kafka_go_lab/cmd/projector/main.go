// Command projector rebuilds "what happened to my order" by reading every
// topic and folding the events into a lookup table.
//
// Nobody writes to this service. It holds no truth of its own — the log is the
// truth, and this is a cache of a fold over it. Two consequences worth
// internalising:
//
//   - If it is wrong, you do not repair it. You throw it away and rebuild it
//     from offset 0 under a new consumer group (scenarios/05_replay).
//   - You can add a second, differently-shaped read model tomorrow — a
//     fraud checker, a revenue dashboard — without touching a single line of
//     the services that produce these events. Adding a *consumer* is free.
//     That asymmetry is most of why people reach for a log.
//
// This is the read side of CQRS, and the staleness is real: an order can be
// confirmed on the log a moment before this table knows it. Eventual
// consistency is not a bug here, it is the price on the ticket.
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
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/segmentio/kafka-go"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/console"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/kafkaio"
)

// view is one row of the read model.
type view struct {
	OrderID  string    `json:"order_id"`
	Customer string    `json:"customer,omitempty"`
	Item     string    `json:"item,omitempty"`
	Qty      int       `json:"qty,omitempty"`
	Amount   string    `json:"amount,omitempty"`
	Status   string    `json:"status"`
	Reason   string    `json:"reason,omitempty"`
	Refunded bool      `json:"refunded,omitempty"`
	Events   int       `json:"events"`
	Updated  time.Time `json:"updated"`
}

// stage ranks how far through its life an order is. Anything unknown ranks 0
// and cannot move the status backwards.
//
// This exists because of a bug worth understanding, since it is invisible
// until the day you rebuild a projection and get a different answer.
//
// This projector reads three topics. Kafka orders records within a partition
// and promises nothing across partitions — let alone across topics. Live, the
// events arrive roughly as they happen, so a naive "last event wins" fold
// looks perfectly correct. On a replay from offset 0 the consumer drains
// whatever is ready, so it can apply StockRejected before the
// PaymentCompleted that preceded it in real time, and the same log folds to a
// different answer.
//
// The fix is not to chase cross-topic ordering; Kafka does not sell it. The
// fix is to make the fold not care. Here the status only ever moves forward,
// so any arrival order lands in the same place. Commutative folds, monotonic
// state, CRDT-shaped thinking: the discipline is that a projection must be a
// function of the SET of events, not of the order you happened to read them.
func stage(status string) int {
	switch status {
	case "placed":
		return 1
	case "paid":
		return 2
	case "confirmed", "cancelled":
		return 3 // terminal
	}
	return 0
}

// advance moves the status forward, never back.
func advance(v view, status, reason string) view {
	if stage(status) <= stage(v.Status) {
		return v
	}
	v.Status = status
	if reason != "" {
		v.Reason = reason
	}
	return v
}

// apply folds one event into a view. It is a pure function — same inputs,
// same output, no clock, no network — which is why it can be tested
// exhaustively in microseconds while the rest of the system needs a broker.
//
// Notice there is no "unknown event type" error. A projector that crashes on
// an event it has not been taught about is a projector that blocks the
// partition the first time someone ships a new event type.
func apply(v view, ev event.Event) view {
	v.OrderID = ev.OrderID
	v.Events++
	// Keep the latest timestamp rather than the last one applied, so the
	// field does not depend on read order either.
	if ev.At.After(v.Updated) {
		v.Updated = ev.At
	}

	switch ev.Type {
	case event.OrderPlaced:
		v.Customer, v.Item, v.Qty = ev.Customer, ev.Item, ev.Qty
		v.Amount = event.USD(ev.Amount)
		v = advance(v, "placed", "")
	case event.PaymentCompleted:
		v = advance(v, "paid", "")
	case event.PaymentFailed:
		v = advance(v, "cancelled", ev.Reason)
	case event.StockReserved:
		v = advance(v, "confirmed", "")
	case event.StockRejected:
		v = advance(v, "cancelled", ev.Reason)
	case event.PaymentRefunded:
		// A boolean that only ever goes false -> true is order-independent
		// for free.
		v.Refunded = true
	}
	return v
}

type model struct {
	mu         sync.RWMutex
	orders     map[string]view
	partitions map[string]int // "topic/partition" -> records seen
}

func newModel() *model {
	return &model{orders: map[string]view{}, partitions: map[string]int{}}
}

func (m *model) record(ev event.Event, msg kafka.Message) view {
	m.mu.Lock()
	defer m.mu.Unlock()
	v := apply(m.orders[ev.OrderID], ev)
	m.orders[ev.OrderID] = v
	m.partitions[fmt.Sprintf("%s/%d", msg.Topic, msg.Partition)]++
	return v
}

func (m *model) snapshot() []view {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]view, 0, len(m.orders))
	for _, v := range m.orders {
		out = append(out, v)
	}
	sort.Slice(out, func(i, j int) bool { return natural(out[i].OrderID) < natural(out[j].OrderID) })
	return out
}

func (m *model) get(id string) (view, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	v, ok := m.orders[id]
	return v, ok
}

func (m *model) stats() map[string]any {
	m.mu.RLock()
	defer m.mu.RUnlock()
	byStatus := map[string]int{}
	total := 0
	for _, v := range m.orders {
		byStatus[v.Status]++
		total += v.Events
	}
	parts := map[string]int{}
	for k, n := range m.partitions {
		parts[k] = n
	}
	return map[string]any{
		"orders":               len(m.orders),
		"events_applied":       total,
		"by_status":            byStatus,
		"records_by_partition": parts,
	}
}

// natural sorts "o-2" before "o-10", which lexicographic order would not.
func natural(id string) int {
	n := 0
	for _, r := range id {
		if r >= '0' && r <= '9' {
			n = n*10 + int(r-'0')
		}
	}
	return n
}

func main() {
	brokers := flag.String("brokers", "localhost:9092", "comma-separated Kafka brokers")
	addr := flag.String("addr", ":8081", "HTTP listen address")
	group := flag.String("group", "projector-svc", "consumer group; a NEW name replays the whole log from the start")
	flag.Parse()

	log := console.New("projector")
	m := newModel()

	handle := func(ctx context.Context, ev event.Event, msg kafka.Message) error {
		v := m.record(ev, msg)
		status := v.Status
		if v.Refunded {
			status += " (refunded)"
		}
		log.Consumed(msg.Topic, msg.Partition, msg.Offset, ev.Type, ev.OrderID+" -> "+status)
		return nil
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "ok") })
	mux.HandleFunc("GET /orders", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, m.snapshot())
	})
	mux.HandleFunc("GET /orders/{id}", func(w http.ResponseWriter, r *http.Request) {
		v, ok := m.get(r.PathValue("id"))
		if !ok {
			// "Not here" and "not yet here" are indistinguishable to a read
			// model. Saying so out loud beats implying the order does not
			// exist.
			http.Error(w, "no such order — or the events for it have not reached me yet", http.StatusNotFound)
			return
		}
		writeJSON(w, v)
	})
	mux.HandleFunc("GET /stats", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, m.stats())
	})

	srv := &http.Server{Addr: *addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Warn("http server stopped: %v", err)
			stop()
		}
	}()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()

	log.Say("serving read model on %s — /orders, /orders/{id}, /stats", *addr)
	if err := kafkaio.Consume(ctx, kafkaio.ConsumerConfig{
		Brokers: strings.Split(*brokers, ","), Group: *group, Topics: event.AllTopics, Log: log,
	}, handle); err != nil {
		log.Warn("stopped: %v", err)
		os.Exit(1)
	}
	log.Say("bye")
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
