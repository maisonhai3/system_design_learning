// Command labctl is the lab's window into the broker: create topics, measure
// consumer lag, and read a topic's raw contents.
//
// These three verbs are not toys. "How far behind is this consumer" and "what
// is actually on the topic" are the two questions you will ask at 3am, and
// knowing how they are *computed* — rather than which dashboard shows them —
// is the difference between debugging Kafka and guessing at it.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/segmentio/kafka-go"

	"github.com/maisonhai3/system_design_learning/kafka_go_lab/internal/event"
)

// The groups this lab runs. Kafka has no registry of "groups that should
// exist" — a group springs into being when a consumer uses the name, so
// anything that reports on them has to be told which names to look for.
var knownGroups = []string{"payment-svc", "payment-refunds", "inventory-svc", "projector-svc"}

func main() {
	brokers := flag.String("brokers", "localhost:9092", "comma-separated Kafka brokers")
	partitions := flag.Int("partitions", 3, "partitions per topic (topics verb)")
	topic := flag.String("topic", event.TopicOrders, "topic to read (dump verb)")
	groups := flag.String("groups", strings.Join(knownGroups, ","), "consumer groups to measure (lag verb)")
	gateway := flag.String("gateway", "http://localhost:8080", "gateway base URL (flood verb)")
	n := flag.Int("n", 20, "how many orders to send (flood verb)")
	item := flag.String("item", "bolt", "item to order (flood verb)")
	flag.Usage = usage
	flag.Parse()

	addrs := strings.Split(*brokers, ",")
	client := &kafka.Client{Addr: kafka.TCP(addrs...), Timeout: 10 * time.Second}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	var err error
	switch flag.Arg(0) {
	case "topics":
		err = createTopics(ctx, client, *partitions)
	case "reset":
		err = reset(ctx, client, *partitions)
	case "lag":
		err = showLag(ctx, client, strings.Split(*groups, ","))
	case "dump":
		err = dump(ctx, client, addrs, *topic)
	case "flood":
		err = flood(ctx, *gateway, *n, *item)
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "labctl:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `labctl <verb> [flags]

verbs:
  topics   create the lab's topics with a fixed partition count
  reset    delete the topics and consumer groups, then recreate them empty
  lag      show, per group and partition, how far behind the consumers are
  dump     print a topic's records straight off the log, oldest first
  flood    POST a burst of orders at the gateway

flags:
`)
	flag.PrintDefaults()
}

// createTopics makes the topics explicitly, because the partition count is a
// decision and not a default.
//
// Partitions are the unit of both ordering and parallelism, and the count is
// awkward to change later: adding partitions rehashes keys to different
// partitions, so a key's history stays on the old one while its future lands
// on a new one — and per-key ordering, the thing you bought partitions for,
// quietly breaks. Pick a number bigger than the consumers you expect.
func createTopics(ctx context.Context, client *kafka.Client, partitions int) error {
	cfgs := make([]kafka.TopicConfig, 0, len(event.AllTopics))
	for _, t := range event.AllTopics {
		cfgs = append(cfgs, kafka.TopicConfig{
			Topic:         t,
			NumPartitions: partitions,
			// One broker in the lab, so one copy. In production this is 3,
			// and it is what stands between you and a dead disk.
			ReplicationFactor: 1,
		})
	}
	res, err := client.CreateTopics(ctx, &kafka.CreateTopicsRequest{Topics: cfgs})
	if err != nil {
		return err
	}
	for name, terr := range res.Errors {
		switch {
		case terr == nil:
			fmt.Printf("created %-10s with %d partitions\n", name, partitions)
		case errors.Is(terr, kafka.TopicAlreadyExists):
			fmt.Printf("%-10s already exists (partition count left alone)\n", name)
		default:
			return fmt.Errorf("creating %s: %w", name, terr)
		}
	}
	return nil
}

// reset gives you a clean log to run a scenario against.
//
// Worth knowing what this really does: deleting a topic throws away the
// records, and deleting a consumer group throws away its committed offsets.
// They are separate pieces of state, and forgetting the second one is why
// "I recreated the topic but my consumer still will not re-read it" is a
// question people ask. Nothing here is something you would do to a production
// cluster; it is here because a lab you cannot re-run twice is a lab you
// stop trusting.
func reset(ctx context.Context, client *kafka.Client, partitions int) error {
	if res, err := client.DeleteGroups(ctx, &kafka.DeleteGroupsRequest{GroupIDs: knownGroups}); err != nil {
		return err
	} else {
		for g, gerr := range res.Errors {
			// A group that never existed, or whose members are gone, is
			// exactly the state we want it in.
			if gerr != nil && !errors.Is(gerr, kafka.GroupIdNotFound) && !errors.Is(gerr, kafka.InvalidGroupId) {
				fmt.Printf("note: could not delete group %s: %v\n", g, gerr)
			}
		}
	}

	if _, err := client.DeleteTopics(ctx, &kafka.DeleteTopicsRequest{Topics: event.AllTopics}); err != nil {
		return err
	}

	// Deletion is asynchronous. Recreating a topic that the broker has not
	// finished removing fails in confusing ways, so wait for it to actually
	// disappear before asking for it back.
	deadline := time.Now().Add(30 * time.Second)
	for {
		meta, err := client.Metadata(ctx, &kafka.MetadataRequest{Topics: event.AllTopics})
		if err != nil {
			return err
		}
		gone := true
		for _, t := range meta.Topics {
			if t.Error == nil && len(t.Partitions) > 0 {
				gone = false
			}
		}
		if gone {
			break
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("topics still present 30s after delete")
		}
		time.Sleep(500 * time.Millisecond)
	}
	fmt.Println("deleted topics and consumer groups")
	return createTopics(ctx, client, partitions)
}

// showLag computes lag the way every Kafka monitoring tool does:
//
//	lag = log end offset - committed offset
//
// It is a count of records, not a duration, and that distinction bites. A lag
// of 10,000 is nothing on a topic doing 100k/s and an outage on one doing
// 10/s. Lag that is flat and non-zero means you are keeping up but started
// behind; lag that climbs means your consumers are slower than your producers
// and no amount of restarting will fix it.
func showLag(ctx context.Context, client *kafka.Client, groups []string) error {
	meta, err := client.Metadata(ctx, &kafka.MetadataRequest{Topics: event.AllTopics})
	if err != nil {
		return err
	}

	ends := map[string]map[int]int64{}   // topic -> partition -> end offset
	starts := map[string]map[int]int64{} // topic -> partition -> first retained offset
	parts := map[string][]int{}
	for _, t := range meta.Topics {
		if t.Error != nil {
			return fmt.Errorf("topic %s: %w", t.Name, t.Error)
		}
		req := &kafka.ListOffsetsRequest{Topics: map[string][]kafka.OffsetRequest{}}
		for _, p := range t.Partitions {
			parts[t.Name] = append(parts[t.Name], p.ID)
			req.Topics[t.Name] = append(req.Topics[t.Name], kafka.FirstOffsetOf(p.ID), kafka.LastOffsetOf(p.ID))
		}
		sort.Ints(parts[t.Name])
		res, err := client.ListOffsets(ctx, req)
		if err != nil {
			return err
		}
		ends[t.Name], starts[t.Name] = map[int]int64{}, map[int]int64{}
		for _, po := range res.Topics[t.Name] {
			// A partition whose leader has not settled yet — which happens
			// for a second or two after a topic is created — reports -1 for
			// "I do not know". Treating that as a real offset produces
			// nonsense like a brand new topic holding one record.
			ends[t.Name][po.Partition] = atLeastZero(po.LastOffset)
			starts[t.Name][po.Partition] = atLeastZero(po.FirstOffset)
		}
	}

	fmt.Printf("%-16s %-10s %3s %10s %10s %8s\n", "GROUP", "TOPIC", "P", "COMMITTED", "END", "LAG")
	for _, g := range groups {
		g = strings.TrimSpace(g)
		if g == "" {
			continue
		}
		res, err := client.OffsetFetch(ctx, &kafka.OffsetFetchRequest{GroupID: g, Topics: parts})
		if err != nil {
			// On a brand new cluster the internal __consumer_offsets topic
			// does not exist until the first group commits, so there is no
			// coordinator to ask yet. That is not a failure, it just means
			// nothing has consumed anything — say so and carry on.
			if errors.Is(err, kafka.NotCoordinatorForGroup) || errors.Is(err, kafka.GroupCoordinatorNotAvailable) {
				fmt.Printf("%-16s %s\n", g, "(no coordinator yet — no consumer group has ever run)")
				continue
			}
			return fmt.Errorf("fetching offsets for %s: %w", g, err)
		}
		if res.Error != nil {
			return fmt.Errorf("group %s: %w", g, res.Error)
		}
		printed := false
		for _, t := range event.AllTopics {
			for _, pf := range res.Topics[t] {
				// -1 means this group has never committed here, which is how
				// you tell "subscribed and caught up" from "not subscribed".
				if pf.CommittedOffset < 0 {
					continue
				}
				end := ends[t][pf.Partition]
				fmt.Printf("%-16s %-10s %3d %10d %10d %8d\n", g, t, pf.Partition, pf.CommittedOffset, end, end-pf.CommittedOffset)
				printed = true
			}
		}
		if !printed {
			fmt.Printf("%-16s %s\n", g, "(no committed offsets — has this group ever run?)")
		}
	}

	fmt.Println()
	for _, t := range event.AllTopics {
		total := int64(0)
		for _, p := range parts[t] {
			total += ends[t][p] - starts[t][p]
		}
		fmt.Printf("%-10s holds %s across %d partitions\n", t, plural(total, "record"), len(parts[t]))
	}
	return nil
}

// dump reads a topic from the beginning with no consumer group at all.
//
// This is the point people miss when they arrive from RabbitMQ or SQS:
// reading does not consume. The records are still there, in order, and
// anybody can come along and read them again. A Kafka topic is a file you
// append to, not a bucket you take from.
func dump(ctx context.Context, client *kafka.Client, brokers []string, topic string) error {
	meta, err := client.Metadata(ctx, &kafka.MetadataRequest{Topics: []string{topic}})
	if err != nil {
		return err
	}
	if len(meta.Topics) == 0 || meta.Topics[0].Error != nil {
		return fmt.Errorf("topic %s not available", topic)
	}

	req := &kafka.ListOffsetsRequest{Topics: map[string][]kafka.OffsetRequest{}}
	for _, p := range meta.Topics[0].Partitions {
		req.Topics[topic] = append(req.Topics[topic], kafka.FirstOffsetOf(p.ID), kafka.LastOffsetOf(p.ID))
	}
	bounds, err := client.ListOffsets(ctx, req)
	if err != nil {
		return err
	}
	ranges := bounds.Topics[topic]
	sort.Slice(ranges, func(i, j int) bool { return ranges[i].Partition < ranges[j].Partition })
	for i := range ranges {
		ranges[i].FirstOffset = atLeastZero(ranges[i].FirstOffset)
		ranges[i].LastOffset = atLeastZero(ranges[i].LastOffset)
	}

	for _, r := range ranges {
		fmt.Printf("\n=== %s partition %d: offsets %d..%d (%s) ===\n",
			topic, r.Partition, r.FirstOffset, r.LastOffset, plural(r.LastOffset-r.FirstOffset, "record"))
		if r.LastOffset <= r.FirstOffset {
			continue
		}
		// No GroupID: this reader is not a group member, commits nothing, and
		// is invisible to everyone else's offsets.
		reader := kafka.NewReader(kafka.ReaderConfig{
			Brokers: brokers, Topic: topic, Partition: r.Partition, MinBytes: 1, MaxBytes: 10e6,
		})
		if err := reader.SetOffset(r.FirstOffset); err != nil {
			reader.Close()
			return err
		}
		for {
			m, err := reader.ReadMessage(ctx)
			if err != nil {
				reader.Close()
				return err
			}
			var ev event.Event
			_ = json.Unmarshal(m.Value, &ev)
			fmt.Printf("  @%-5d key=%-6s %-17s %s\n", m.Offset, string(m.Key), ev.Type, summarise(ev))
			if m.Offset >= r.LastOffset-1 {
				break
			}
		}
		reader.Close()
	}
	return nil
}

// atLeastZero turns Kafka's "unknown offset" sentinel into something safe to
// do arithmetic with.
func atLeastZero(offset int64) int64 {
	if offset < 0 {
		return 0
	}
	return offset
}

func plural(n int64, word string) string {
	if n == 1 {
		return fmt.Sprintf("%d %s", n, word)
	}
	return fmt.Sprintf("%d %ss", n, word)
}

func summarise(ev event.Event) string {
	var b strings.Builder
	if ev.Item != "" {
		fmt.Fprintf(&b, "%s x%d ", ev.Item, ev.Qty)
	}
	if ev.Amount != 0 {
		fmt.Fprintf(&b, "%s ", event.USD(ev.Amount))
	}
	if ev.Reason != "" {
		fmt.Fprintf(&b, "(%s)", ev.Reason)
	}
	return strings.TrimSpace(b.String())
}

// flood posts a burst of orders so there is something to watch.
func flood(ctx context.Context, base string, n int, item string) error {
	var wg sync.WaitGroup
	sem := make(chan struct{}, 8)
	var mu sync.Mutex
	failures := 0

	started := time.Now()
	for i := range n {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int) {
			defer wg.Done()
			defer func() { <-sem }()
			body, _ := json.Marshal(map[string]any{
				"customer": fmt.Sprintf("customer-%d", i%5),
				"item":     item,
				"qty":      1,
			})
			req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/orders", bytes.NewReader(body))
			if err != nil {
				mu.Lock()
				failures++
				mu.Unlock()
				return
			}
			req.Header.Set("Content-Type", "application/json")
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				mu.Lock()
				failures++
				mu.Unlock()
				return
			}
			resp.Body.Close()
			if resp.StatusCode != http.StatusAccepted {
				mu.Lock()
				failures++
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	fmt.Printf("sent %d orders in %s (%d failed)\n", n, time.Since(started).Round(time.Millisecond), failures)
	if failures > 0 {
		return fmt.Errorf("%d of %d orders were not accepted — is the gateway up?", failures, n)
	}
	return nil
}
