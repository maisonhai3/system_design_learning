// Command inventory reserves stock for orders that have been paid for.
//
// It consumes the payments topic, not the orders topic. That single choice is
// the pipeline: "reserve stock only for orders that are paid" is expressed by
// *what you subscribe to*, not by an if-statement or a call to the payment
// service. Rewiring the business process means rewiring subscriptions.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"

	"github.com/segmentio/kafka-go"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/console"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/kafkaio"
)

// warehouse is this service's private state. Note "doohickey: 0" — the lab
// needs a reliable way to fail late, after the money has already moved.
type warehouse struct {
	mu  sync.Mutex
	qty map[string]int
}

func newWarehouse() *warehouse {
	return &warehouse{qty: map[string]int{"bolt": 100000, "widget": 10, "gizmo": 5, "doohickey": 0}}
}

// reserve takes stock if there is enough, and reports what happened.
//
// The check and the decrement are one critical section. Splitting them would
// let two orders both see "1 left" and both take it — the oversell bug, which
// is the same race as a double charge wearing different clothes.
func (w *warehouse) reserve(item string, qty int) (ok bool, left int, reason string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	have, known := w.qty[item]
	if !known {
		return false, 0, fmt.Sprintf("we do not stock %q", item)
	}
	if have < qty {
		return false, have, fmt.Sprintf("only %d %s left, wanted %d", have, item, qty)
	}
	w.qty[item] = have - qty
	return true, w.qty[item], ""
}

func main() {
	brokers := flag.String("brokers", "localhost:9092", "comma-separated Kafka brokers")
	instance := flag.String("instance", "1", "instance label, so you can tell replicas apart in the log")
	flag.Parse()

	log := console.New("inventory#" + *instance)
	addrs := strings.Split(*brokers, ",")
	prod := kafkaio.NewProducer(addrs, log)
	defer prod.Close()

	shelf := newWarehouse()

	handle := func(ctx context.Context, ev event.Event, m kafka.Message) error {
		// We see every record on the payments topic, including the failures
		// and the refunds. Ignoring what you don't care about is normal and
		// cheap; a consumer that must understand every event type on a topic
		// is a consumer that breaks every time someone adds one.
		if ev.Type != event.PaymentCompleted {
			return nil
		}
		log.Consumed(m.Topic, m.Partition, m.Offset, ev.Type,
			fmt.Sprintf("%s %s x%d", ev.OrderID, ev.Item, ev.Qty))

		ok, left, reason := shelf.reserve(ev.Item, ev.Qty)
		if !ok {
			log.Say("%s REJECTED: %s", ev.OrderID, reason)
			out := event.New(event.StockRejected, ev.OrderID)
			// Carry the amount so payment knows what to refund without
			// looking anything up.
			out.Item, out.Qty, out.Amount, out.Reason = ev.Item, ev.Qty, ev.Amount, reason
			return prod.Publish(ctx, event.TopicStock, out)
		}

		log.Say("%s reserved %d %s (%d left on the shelf)", ev.OrderID, ev.Qty, ev.Item, left)
		out := event.New(event.StockReserved, ev.OrderID)
		out.Item, out.Qty, out.Amount = ev.Item, ev.Qty, ev.Amount
		return prod.Publish(ctx, event.TopicStock, out)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	log.Say("shelf: bolt=100000 widget=10 gizmo=5 doohickey=0")
	if err := kafkaio.Consume(ctx, kafkaio.ConsumerConfig{
		Brokers: addrs, Group: "inventory-svc", Topics: []string{event.TopicPayments}, Log: log,
	}, handle); err != nil {
		log.Warn("stopped: %v", err)
		os.Exit(1)
	}
	log.Say("bye")
}
