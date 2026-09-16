package kafkaio

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"time"

	"github.com/segmentio/kafka-go"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/console"
	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
)

// Handler does the actual work for one record. Returning an error stops the
// consumer without committing, so the record will be delivered again.
type Handler func(ctx context.Context, ev event.Event, m kafka.Message) error

// ConsumerConfig describes one subscription.
type ConsumerConfig struct {
	Brokers []string

	// Group is the consumer group name, and it is the most important string
	// in this file. Kafka gives every group its own independent cursor into
	// the same log:
	//
	//   - members of the SAME group split the partitions between them
	//     (that is how you scale out — see scenarios/02_scale_out)
	//   - DIFFERENT groups each get every record
	//     (that is how you fan out — payment, inventory and the projector all
	//     read the same topics without knowing about each other)
	//
	// Change this string and you get a brand new reader of the same history.
	// That is the whole trick behind scenarios/05_replay.
	Group string

	Topics []string
	Log    *console.Logger

	// CrashAfter, when > 0, kills the process after doing the work for that
	// many records but *before* committing the offset. It exists to make
	// at-least-once delivery something you watch rather than something you
	// read about. See scenarios/04_duplicates.
	CrashAfter int
}

// Consume runs the fetch/handle/commit loop until ctx is cancelled.
func Consume(ctx context.Context, cfg ConsumerConfig, h Handler) error {
	rc := kafka.ReaderConfig{
		Brokers: cfg.Brokers,
		GroupID: cfg.Group,

		// StartOffset only applies when this group has NEVER committed an
		// offset for a partition. Once the group has a committed offset,
		// Kafka resumes from there and this field is ignored. People lose
		// hours to this: "I set FirstOffset, why didn't it replay?" Because
		// the group remembered where it was. To genuinely start over, use a
		// new group name.
		StartOffset: kafka.FirstOffset,

		// Don't wait to accumulate a big fetch; we want to see records land.
		MinBytes: 1,
		MaxBytes: 10e6,
		MaxWait:  200 * time.Millisecond,

		// 0 means "only commit when CommitMessages is called". The
		// alternative — a background commit every N seconds — is faster and
		// makes the duplicate window bigger and fuzzier. We want the commit
		// point to be somewhere we chose.
		CommitInterval: 0,

		// Notice when partitions are added or reassigned rather than sitting
		// on a stale assignment.
		WatchPartitionChanges: true,

		// Failure detection, tuned down from the 30s defaults so this lab
		// moves at the speed of a person watching it.
		//
		// The trade is real and you will meet it in production. Kafka cannot
		// tell a dead consumer from a slow one; all it has is heartbeats. Long
		// timeouts mean a crashed consumer's partitions sit unserved for that
		// long — which is why restarting a consumer feels like it hangs.
		// Short timeouts reclaim work quickly but will evict a consumer that
		// was merely busy, triggering a rebalance that stops *every* consumer
		// in the group and makes the backlog worse. Tuning these down is not
		// free; it is buying faster failover with a higher chance of
		// pointless rebalances.
		HeartbeatInterval: 1 * time.Second,
		SessionTimeout:    6 * time.Second,
		RebalanceTimeout:  6 * time.Second,
		JoinGroupBackoff:  1 * time.Second,
	}
	// kafka-go wants exactly one of these set.
	if len(cfg.Topics) == 1 {
		rc.Topic = cfg.Topics[0]
	} else {
		rc.GroupTopics = cfg.Topics
	}

	r := kafka.NewReader(rc)
	defer r.Close()

	cfg.Log.Say("joined group %q on %v", cfg.Group, cfg.Topics)

	done := 0
	for {
		// FetchMessage hands us a record and does NOT move the committed
		// offset. The gap between this line and CommitMessages below is
		// where every delivery-semantics conversation actually happens.
		m, err := r.FetchMessage(ctx)
		if err != nil {
			if errors.Is(err, context.Canceled) || errors.Is(ctx.Err(), context.Canceled) {
				cfg.Log.Say("leaving group %q", cfg.Group)
				return nil
			}
			return err
		}

		var ev event.Event
		if err := json.Unmarshal(m.Value, &ev); err != nil {
			// A record we cannot parse will never become parseable. Retrying
			// it forever blocks the partition behind it — the classic
			// "poison pill" outage. Real systems route it to a dead-letter
			// topic; the toy version is: complain, skip, keep moving.
			cfg.Log.Warn("undecodable record at %s/%d@%d, skipping: %v", m.Topic, m.Partition, m.Offset, err)
			if err := r.CommitMessages(ctx, m); err != nil {
				return err
			}
			continue
		}

		if err := h(ctx, ev, m); err != nil {
			return err
		}
		done++

		if cfg.CrashAfter > 0 && done >= cfg.CrashAfter {
			cfg.Log.Warn("CRASH: work done for %s/%d@%d, offset NOT committed", m.Topic, m.Partition, m.Offset)
			cfg.Log.Warn("whoever restarts me will be handed this record again")
			os.Exit(1)
		}

		// Commit AFTER the work. That ordering is the choice:
		//
		//   commit after work  -> at-least-once. Crash in between and the
		//                         record is redelivered. Duplicates possible,
		//                         loss is not.
		//   commit before work -> at-most-once. Crash in between and the
		//                         record is gone. Loss possible, duplicates
		//                         are not.
		//
		// There is no third option that a distributed system gives you for
		// free. Since you cannot have neither, pick duplicates — because
		// duplicates you can defend against with an idempotency key, and lost
		// money you cannot get back.
		if err := r.CommitMessages(ctx, m); err != nil {
			return err
		}
	}
}
