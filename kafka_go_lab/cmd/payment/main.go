// Command payment decides whether an order gets paid for, and refunds itself
// when inventory later says the goods do not exist.
//
// Note what is NOT here: any mention of the inventory service, the projector,
// or the gateway. This service knows two topic names and its own rules. You
// could delete the inventory service entirely and payment would keep working,
// which is the actual test of whether services are coupled — not whether they
// live in separate repos.
//
// It also runs TWO consumer groups, and that is deliberate. A consumer group's
// subscription is a set of topics; if you point two differently-shaped
// subscriptions at one group name, members fight over assignments. One group
// per subscription is the rule.
package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"

	"github.com/segmentio/kafka-go"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/console"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/dedupe"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/kafkaio"
)

// limitCents is this service's entire risk policy. Deterministic, not random,
// so the lab tells the same story every time you run it.
const limitCents = 50000 // $500.00

// decide is the business rule, extracted from all the plumbing so it can be
// tested without a broker. If you can't unit test your domain logic without
// standing up infrastructure, the logic is in the wrong place.
func decide(ev event.Event) (approved bool, reason string) {
	switch {
	case ev.Customer == "broke":
		return false, "customer has no funds"
	case ev.Amount > limitCents:
		return false, fmt.Sprintf("%s is over the %s single-order limit", event.USD(ev.Amount), event.USD(limitCents))
	default:
		return true, ""
	}
}

// ledger is the side effect — the thing that must not happen twice.
//
// It is on disk rather than in a map because a ledger that forgets when the
// process dies cannot tell you that it charged the customer twice: the second
// charge looks like the first one. Real money lives in a database for exactly
// this reason, and the lab would quietly lie to you without it.
type ledger struct {
	mu      sync.Mutex
	charged map[string]int
	file    *os.File
}

func newLedger() *ledger { return &ledger{charged: map[string]int{}} }

// openLedger replays an append-only file of movements, then keeps appending.
func openLedger(path string) (*ledger, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	l := newLedger()
	if f, err := os.Open(path); err == nil {
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			var orderID string
			var cents int
			// Each line is "order cents"; anything malformed means the file
			// is not what we think it is, so stop rather than guess.
			if _, err := fmt.Sscanf(sc.Text(), "%s %d", &orderID, &cents); err != nil {
				continue
			}
			l.charged[orderID] += cents
		}
		err := sc.Err()
		f.Close()
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}

	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	l.file = f
	return l, nil
}

func (l *ledger) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return nil
	}
	err := l.file.Close()
	l.file = nil
	return err
}

// apply moves money and returns the running total for that order.
func (l *ledger) apply(orderID string, cents int) int {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.charged[orderID] += cents
	if l.file != nil {
		if _, err := fmt.Fprintf(l.file, "%s %d\n", orderID, cents); err == nil {
			_ = l.file.Sync()
		}
	}
	return l.charged[orderID]
}

func main() {
	brokers := flag.String("brokers", "localhost:9092", "comma-separated Kafka brokers")
	instance := flag.String("instance", "1", "instance label, so you can tell replicas apart in the log")
	crashAfter := flag.Int("crash-after", 0, "kill this process after N records, before committing (0 = never)")
	idempotent := flag.Bool("idempotent", false, "ignore an event ID that has already been applied")
	stateDir := flag.String("state-dir", "state", "where the ledger and the idempotency table live")
	flag.Parse()

	log := console.New("payment#" + *instance)
	addrs := strings.Split(*brokers, ",")
	prod := kafkaio.NewProducer(addrs, log)
	defer prod.Close()

	book, err := openLedger(filepath.Join(*stateDir, "ledger-"+*instance+".log"))
	if err != nil {
		log.Warn("cannot open ledger: %v", err)
		os.Exit(1)
	}
	defer book.Close()

	guard, err := dedupe.Open(filepath.Join(*stateDir, "applied-"+*instance+".log"), 4096)
	if err != nil {
		log.Warn("cannot open idempotency table: %v", err)
		os.Exit(1)
	}
	defer guard.Close()

	if *idempotent {
		log.Say("idempotency guard ON — a repeated event ID will not be charged twice")
	} else {
		log.Say("idempotency guard OFF — a redelivered record will be charged again")
	}

	// charge handles orders.
	charge := func(ctx context.Context, ev event.Event, m kafka.Message) error {
		if ev.Type != event.OrderPlaced {
			return nil
		}
		log.Consumed(m.Topic, m.Partition, m.Offset, ev.Type,
			fmt.Sprintf("%s %s x%d %s", ev.OrderID, ev.Item, ev.Qty, event.USD(ev.Amount)))

		if *idempotent && !guard.FirstTime(ev.ID) {
			// The record arrived again. The fact has not changed, so neither
			// should the world. Note we still return nil, so the offset gets
			// committed and we move on.
			log.Warn("event %s already applied — skipping (this is the guard earning its keep)", ev.ID)
			return nil
		}

		approved, reason := decide(ev)
		if !approved {
			log.Say("%s DECLINED: %s", ev.OrderID, reason)
			out := event.New(event.PaymentFailed, ev.OrderID)
			out.Item, out.Qty, out.Amount, out.Reason = ev.Item, ev.Qty, ev.Amount, reason
			return prod.Publish(ctx, event.TopicPayments, out)
		}

		total := book.apply(ev.OrderID, ev.Amount)
		if total > ev.Amount {
			log.Warn("DOUBLE CHARGE on %s: charged %s again, customer has now paid %s",
				ev.OrderID, event.USD(ev.Amount), event.USD(total))
		} else {
			log.Say("%s charged %s", ev.OrderID, event.USD(ev.Amount))
		}

		out := event.New(event.PaymentCompleted, ev.OrderID)
		// Carry the details forward: inventory reads this topic and has no
		// way to look up the original order.
		out.Customer, out.Item, out.Qty, out.Amount = ev.Customer, ev.Item, ev.Qty, ev.Amount
		return prod.Publish(ctx, event.TopicPayments, out)
	}

	// refund handles the compensating half of the saga. Nobody orchestrates
	// this. Inventory publishes "I rejected that stock" as a plain fact, and
	// payment happens to care. There is no coordinator to be the single point
	// of failure — and equally, there is no single place to look to find out
	// what state an order is in. That is the trade choreography makes.
	refund := func(ctx context.Context, ev event.Event, m kafka.Message) error {
		if ev.Type != event.StockRejected {
			return nil
		}
		log.Consumed(m.Topic, m.Partition, m.Offset, ev.Type, ev.OrderID+" "+ev.Reason)
		total := book.apply(ev.OrderID, -ev.Amount)
		log.Say("%s refunded %s (balance now %s)", ev.OrderID, event.USD(ev.Amount), event.USD(total))
		out := event.New(event.PaymentRefunded, ev.OrderID)
		out.Amount, out.Reason = ev.Amount, ev.Reason
		return prod.Publish(ctx, event.TopicPayments, out)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var wg sync.WaitGroup
	start := func(cfg kafkaio.ConsumerConfig, h kafkaio.Handler) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := kafkaio.Consume(ctx, cfg, h); err != nil {
				log.Warn("consumer %q stopped: %v", cfg.Group, err)
				stop()
			}
		}()
	}

	start(kafkaio.ConsumerConfig{
		Brokers: addrs, Group: "payment-svc", Topics: []string{event.TopicOrders},
		Log: log, CrashAfter: *crashAfter,
	}, charge)

	start(kafkaio.ConsumerConfig{
		Brokers: addrs, Group: "payment-refunds", Topics: []string{event.TopicStock},
		Log: log,
	}, refund)

	wg.Wait()
	log.Say("bye")
}
