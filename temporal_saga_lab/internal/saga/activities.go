package saga

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/google/uuid"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/inventory"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/payments"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/shipping"
)

// KeyMode decides what the activities do with the idempotency key the workflow
// hands them. It exists so scenario 02 can show the same code being safe and
// unsafe with one environment variable.
//
//	stable       use the key the workflow computed. Same key on every attempt.
//	per-attempt  generate a fresh uuid here, inside the activity. This is not a
//	             strawman — it is what you write when you reach for a unique id
//	             at the place that needs one, and it is wrong for a reason that
//	             is invisible until an activity is retried.
//	off          send no key at all, and find out what your downstream does.
type KeyMode string

const (
	KeyStable     KeyMode = "stable"
	KeyPerAttempt KeyMode = "per-attempt"
	KeyOff        KeyMode = "off"
)

// Activities is the only part of this system that is allowed to touch the
// network. Workflow code cannot: it has to be replayable, and the network is
// not.
type Activities struct {
	Client       *labhttp.Client
	PaymentsURL  string
	InventoryURL string
	ShippingURL  string
	KeyMode      KeyMode
}

func KeyModeFromEnv() KeyMode {
	switch KeyMode(strings.ToLower(os.Getenv("LAB_IDEMPOTENCY"))) {
	case KeyPerAttempt:
		return KeyPerAttempt
	case KeyOff:
		return KeyOff
	default:
		return KeyStable
	}
}

// key decides what actually goes on the wire.
//
// Note which of the three branches the WORKFLOW could have implemented itself:
// only two. A workflow cannot produce a fresh value per attempt — that is what
// determinism means, and it is why the per-attempt bug has to be injected down
// here. The place that is allowed to generate a unique id is precisely the
// place whose ids are not stable across retries, which is a small, permanent,
// and easy-to-miss trap.
func (a *Activities) key(mode KeyMode, given string) string {
	if mode == "" {
		mode = a.KeyMode
	}
	switch mode {
	case KeyOff:
		return ""
	case KeyPerAttempt:
		return uuid.NewString()
	default:
		return given
	}
}

// wrap converts a transport error into something Temporal can act on.
//
// This is the one adapter that matters. Temporal's retry policy retries every
// error by default; the only way to stop it is to say so. A 4xx from a service
// is a decision — "this order asks for ten of something we have four of" — and
// re-asking will not change it. Marking it non-retryable turns a two-minute
// death by retry budget into a two-second failure with the right message.
func wrap(err error, what string) error {
	if err == nil {
		return nil
	}
	var he *labhttp.Error
	if errors.As(err, &he) && !he.Retryable() {
		return temporal.NewNonRetryableApplicationError(
			fmt.Sprintf("%s rejected: %s", what, he.Msg), he.Code, nil)
	}
	return fmt.Errorf("%s: %w", what, err)
}

// ReserveInventory holds stock for the order.
type ReserveInput struct {
	OrderID        string
	SKU            string
	Qty            int
	IdempotencyKey string
	KeyMode        KeyMode
}

func (a *Activities) ReserveInventory(ctx context.Context, in ReserveInput) (string, error) {
	var res inventory.Reservation
	err := a.Client.Post(ctx, a.InventoryURL+"/reservations", inventory.ReserveRequest{
		OrderID: in.OrderID, SKU: in.SKU, Qty: in.Qty, IdempotencyKey: a.key(in.KeyMode, in.IdempotencyKey),
	}, &res)
	if err != nil {
		return "", wrap(err, "reserve inventory")
	}
	activity.GetLogger(ctx).Info("reserved", "id", res.ID, "replayed", res.Replayed)
	return res.ID, nil
}

// ReleaseInventory is ReserveInventory's compensation.
func (a *Activities) ReleaseInventory(ctx context.Context, reservationID string) error {
	err := a.Client.Post(ctx, a.InventoryURL+"/reservations/"+reservationID+"/release", nil, nil)
	return wrap(err, "release inventory")
}

type ChargeInput struct {
	OrderID        string
	AmountCents    int64
	IdempotencyKey string
	KeyMode        KeyMode
}

type ChargeResult struct {
	ChargeID string
	Replayed bool
}

func (a *Activities) ChargePayment(ctx context.Context, in ChargeInput) (ChargeResult, error) {
	var ch payments.Charge
	err := a.Client.Post(ctx, a.PaymentsURL+"/charges", payments.ChargeRequest{
		OrderID: in.OrderID, AmountCents: in.AmountCents, IdempotencyKey: a.key(in.KeyMode, in.IdempotencyKey),
	}, &ch)
	if err != nil {
		return ChargeResult{}, wrap(err, "charge payment")
	}
	activity.GetLogger(ctx).Info("charged", "id", ch.ID, "replayed", ch.Replayed)
	return ChargeResult{ChargeID: ch.ID, Replayed: ch.Replayed}, nil
}

type RefundInput struct {
	OrderID        string
	ChargeID       string
	IdempotencyKey string
	KeyMode        KeyMode
}

// RefundPayment is ChargePayment's compensation — and note what it is not.
// It does not undo the charge; it adds a second, opposite row. The customer's
// statement will show both. A saga cannot make things un-happen, it can only
// make them net to zero, and the difference leaks all the way to the customer.
func (a *Activities) RefundPayment(ctx context.Context, in RefundInput) error {
	err := a.Client.Post(ctx, a.PaymentsURL+"/refunds", payments.RefundRequest{
		ChargeID: in.ChargeID, OrderID: in.OrderID, IdempotencyKey: a.key(in.KeyMode, in.IdempotencyKey),
	}, nil)
	return wrap(err, "refund payment")
}

type ShipInput struct {
	OrderID        string
	Address        string
	IdempotencyKey string
	KeyMode        KeyMode
}

func (a *Activities) CreateShipment(ctx context.Context, in ShipInput) (string, error) {
	// A heartbeat is how a long activity says "still here". This one finishes
	// in milliseconds so it is only a gesture — but the rule it stands for is
	// real: without heartbeats, a worker that dies mid-activity is not noticed
	// until StartToCloseTimeout expires, however long that is.
	activity.RecordHeartbeat(ctx, "calling carrier")

	var sh shipping.Shipment
	err := a.Client.Post(ctx, a.ShippingURL+"/shipments", shipping.ShipRequest{
		OrderID: in.OrderID, Address: in.Address, IdempotencyKey: a.key(in.KeyMode, in.IdempotencyKey),
	}, &sh)
	if err != nil {
		return "", wrap(err, "create shipment")
	}
	return sh.ID, nil
}

// FraudCheck exists only for scenario 05. It is the new step somebody adds to
// the workflow six months in, without thinking about the workflows that are
// already running.
func (a *Activities) FraudCheck(ctx context.Context, orderID string) (bool, error) {
	return true, nil
}
