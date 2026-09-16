// Package event is the contract.
//
// It is the only code every service shares, and it deliberately contains no
// behaviour — just the shape of what goes on the log and the names of the logs
// it goes to. That restraint is the point: the moment this package grows a
// function that "decides" something, two services start sharing a decision,
// and you have a distributed monolith wearing a microservice costume.
//
// In a real system this would be a schema (Avro/Protobuf) registered in a
// schema registry, and the registry would enforce that you can add a field
// without breaking consumers that have never heard of it. JSON plus Go's
// "unknown fields are ignored" is the toy version of the same promise.
package event

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"
)

// Topics. A topic is a named, append-only log. That is the whole idea.
// Everything else Kafka does is bookkeeping on top of "append here, read from
// position N".
const (
	TopicOrders   = "orders"   // what the outside world asked for
	TopicPayments = "payments" // what the payment service decided
	TopicStock    = "stock"    // what the inventory service decided
)

// AllTopics is what the projector subscribes to: it wants the whole story.
var AllTopics = []string{TopicOrders, TopicPayments, TopicStock}

// Event types. Past tense, always: an event is a fact that already happened.
// If you find yourself naming one "ReserveStock" you have written a command,
// not an event, and you have quietly re-invented RPC with extra steps.
const (
	OrderPlaced      = "OrderPlaced"
	PaymentCompleted = "PaymentCompleted"
	PaymentFailed    = "PaymentFailed"
	PaymentRefunded  = "PaymentRefunded"
	StockReserved    = "StockReserved"
	StockRejected    = "StockRejected"
)

// Event is one record on the log.
type Event struct {
	// ID is unique per event. It is *not* the order ID: if the same fact is
	// delivered to you twice (and it will be — see scenarios/04_duplicates),
	// both copies carry the same ID. That is what makes de-duplication
	// possible at all.
	ID string `json:"id"`

	Type string `json:"type"`

	// OrderID is used as the Kafka message key, which decides the partition,
	// which is what buys us ordering per order. See kafkaio.Producer.
	OrderID string `json:"order_id"`

	// The payload is denormalised on purpose. Inventory needs the item and
	// quantity, but it consumes the *payments* topic, not orders. Either the
	// payment service copies those fields forward, or inventory has to call
	// back to someone to ask — and then you have rebuilt the synchronous
	// dependency you went asynchronous to escape.
	Customer string `json:"customer,omitempty"`
	Item     string `json:"item,omitempty"`
	Qty      int    `json:"qty,omitempty"`
	Amount   int    `json:"amount,omitempty"` // in cents; never use floats for money

	Reason string    `json:"reason,omitempty"`
	At     time.Time `json:"at"`
}

// New stamps a fresh event. Callers fill in the payload fields they own.
func New(typ, orderID string) Event {
	return Event{ID: NewID(), Type: typ, OrderID: orderID, At: time.Now().UTC()}
}

// NewID returns a short random identifier. Random, not sequential, because
// nothing in a distributed system gets to assume a single counter.
func NewID() string {
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing means the machine is broken; there is no
		// sensible fallback that preserves the uniqueness we depend on.
		panic("event: out of randomness: " + err.Error())
	}
	return hex.EncodeToString(b[:])
}

// USD renders cents for humans. Display only.
func USD(cents int) string {
	sign := ""
	if cents < 0 {
		sign, cents = "-", -cents
	}
	return fmt.Sprintf("%s$%d.%02d", sign, cents/100, cents%100)
}
