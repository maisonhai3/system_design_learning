// Package kafkaio wraps the Kafka client so the services can stay about the
// business and the interesting settings live in one readable place.
package kafkaio

import (
	"context"
	"encoding/json"
	"time"

	"github.com/segmentio/kafka-go"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/console"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
)

// Producer appends events to topics.
type Producer struct {
	w *kafka.Writer
}

// NewProducer builds a writer that can publish to any topic. Every setting
// below is a decision, so each one is explained.
func NewProducer(brokers []string, log *console.Logger) *Producer {
	w := &kafka.Writer{
		Addr: kafka.TCP(brokers...),

		// Hash: partition = hash(key) % partitionCount. The same key always
		// lands on the same partition, and a partition is the *only* unit
		// Kafka orders. "Kafka guarantees ordering" is false. "Kafka
		// guarantees ordering within a partition" is true, and choosing the
		// key is how you decide what gets ordered.
		//
		// We key by order ID: everything about order o-7 is in sequence,
		// while o-7 and o-8 are free to be processed in parallel. That is the
		// trade you are actually making — ordering costs you concurrency, so
		// buy exactly as much of it as the domain needs.
		Balancer: &kafka.Hash{},

		// RequireAll: don't call it written until every in-sync replica has
		// it. Free on this one-broker lab; on a real cluster this is the knob
		// where you trade latency for durability. RequireOne is faster and
		// loses data when the leader dies before replicating. RequireNone is
		// fire-and-forget.
		RequiredAcks: kafka.RequireAll,

		// The default is 1 second: the writer sits on a message that long
		// hoping to batch it with friends. Excellent for throughput, terrible
		// for a lab where you are watching a terminal waiting for something
		// to happen. Batching is a throughput/latency dial, and here we want
		// latency.
		BatchTimeout: 10 * time.Millisecond,

		// Let a typo'd topic name fail loudly instead of silently creating a
		// brand new single-partition topic that quietly breaks your ordering
		// assumptions. Auto-creation is convenience that costs correctness.
		AllowAutoTopicCreation: false,

		// Called once the broker has acknowledged the write, which is the
		// first moment the partition and offset actually exist. This is why
		// the produced lines in your terminal can show a real offset.
		Completion: func(msgs []kafka.Message, err error) {
			for _, m := range msgs {
				if err != nil {
					log.Warn("produce to %s FAILED: %v", m.Topic, err)
					continue
				}
				var ev event.Event
				if json.Unmarshal(m.Value, &ev) == nil {
					log.Produced(m.Topic, m.Partition, m.Offset, ev.Type, ev.OrderID)
				}
			}
		},
	}
	return &Producer{w: w}
}

// Publish appends one event. It blocks until the broker acknowledges, so a
// returned nil error means the record is durably on the log.
func (p *Producer) Publish(ctx context.Context, topic string, ev event.Event) error {
	b, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	return p.w.WriteMessages(ctx, kafka.Message{
		Topic: topic,
		Key:   []byte(ev.OrderID), // the key picks the partition
		Value: b,
	})
}

func (p *Producer) Close() error { return p.w.Close() }
