// Package payments is the service that can charge you twice.
//
// It is the smallest service in the lab and the only one where a duplicate is
// expensive, which makes it the right place to learn what an idempotency key
// is actually for.
package payments

import (
	"database/sql"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/chaos"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/store"
)

// The UNIQUE on idempotency_key is the whole safety mechanism.
//
// Note that SQLite — like PostgreSQL, and like the SQL standard — treats NULLs
// as distinct in a UNIQUE index. So a charge with no key collides with nothing,
// and you can insert a thousand of them. That is not a quirk to work around: it
// is an accurate encoding of the rule. No key, no protection.
const DDL = `
CREATE TABLE IF NOT EXISTS charges (
  id              TEXT PRIMARY KEY,
  order_id        TEXT NOT NULL,
  amount_cents    INTEGER NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS charges_order ON charges(order_id);

CREATE TABLE IF NOT EXISTS refunds (
  id              TEXT PRIMARY KEY,
  charge_id       TEXT NOT NULL,
  order_id        TEXT NOT NULL,
  amount_cents    INTEGER NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refunds_order ON refunds(order_id);
`

type Service struct {
	db    *store.DB
	chaos *chaos.Controller
}

func New(db *store.DB) *Service { return &Service{db: db, chaos: chaos.New()} }

type ChargeRequest struct {
	OrderID        string `json:"order_id"`
	AmountCents    int64  `json:"amount_cents"`
	IdempotencyKey string `json:"idempotency_key,omitempty"`
}

type Charge struct {
	ID          string `json:"id"`
	OrderID     string `json:"order_id"`
	AmountCents int64  `json:"amount_cents"`
	CreatedAt   string `json:"created_at"`
	// Replayed is true when this response describes a charge that already
	// existed. The caller asked twice and was billed once — which is the
	// outcome an idempotency key buys, and worth saying out loud in the API
	// rather than hiding behind an indistinguishable 200.
	Replayed bool `json:"replayed"`
}

type Refund struct {
	ID          string `json:"id"`
	ChargeID    string `json:"charge_id"`
	AmountCents int64  `json:"amount_cents"`
	Replayed    bool   `json:"replayed"`
}

type Ledger struct {
	OrderID       string `json:"order_id"`
	Charges       int    `json:"charges"`
	ChargedCents  int64  `json:"charged_cents"`
	Refunds       int    `json:"refunds"`
	RefundedCents int64  `json:"refunded_cents"`
	NetCents      int64  `json:"net_cents"`
}

func (s *Service) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.Handle("/_chaos", s.chaos.Handler())
	mux.HandleFunc("POST /charges", s.charge)
	mux.HandleFunc("POST /refunds", s.refund)
	mux.HandleFunc("GET /ledger", s.ledger)
	mux.HandleFunc("DELETE /_all", s.truncate)
	return labhttp.Log("payments ", mux)
}

func (s *Service) charge(w http.ResponseWriter, r *http.Request) {
	var req ChargeRequest
	if err := labhttp.ReadJSON(r, &req); err != nil {
		labhttp.Fail(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if req.AmountCents <= 0 {
		// A business rejection: 4xx, because trying again cannot change it.
		labhttp.Fail(w, http.StatusUnprocessableEntity, "invalid_amount", "amount must be positive")
		return
	}

	d := s.chaos.Take("charge")
	d.Wait()
	if d.Mode == chaos.BeforeCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "injected failure BEFORE the charge was written")
		return
	}

	ch, err := s.insertCharge(req)
	if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}

	if d.Mode == chaos.AfterCommit {
		// The money moved. The caller is about to be told it did not.
		//
		// This is the single most important line in the lab. Everything the
		// orchestrator does next — retry, give up, compensate — is done on
		// information that is now wrong.
		labhttp.Fail(w, http.StatusInternalServerError, "chaos",
			"injected failure AFTER the charge was written (charge "+ch.ID+" exists)")
		return
	}
	labhttp.WriteJSON(w, http.StatusOK, ch)
}

func (s *Service) insertCharge(req ChargeRequest) (Charge, error) {
	ch := Charge{
		ID:          "chg_" + uuid.NewString()[:8],
		OrderID:     req.OrderID,
		AmountCents: req.AmountCents,
		CreatedAt:   time.Now().UTC().Format(time.RFC3339Nano),
	}
	var key any // nil -> SQL NULL -> collides with nothing
	if req.IdempotencyKey != "" {
		key = req.IdempotencyKey
	}
	_, err := s.db.Exec(
		`INSERT INTO charges (id, order_id, amount_cents, idempotency_key, created_at) VALUES (?,?,?,?,?)`,
		ch.ID, ch.OrderID, ch.AmountCents, key, ch.CreatedAt)
	if err == nil {
		return ch, nil
	}
	if !store.IsUnique(err) {
		return Charge{}, err
	}
	// Someone already charged with this key. Return THAT charge, unchanged.
	//
	// Returning 200 with the original charge — rather than 409 — is the choice
	// that makes retries boring: the caller cannot tell whether it was the
	// first or the fourth attempt, which is exactly what it should not have to
	// care about.
	var got Charge
	err = s.db.QueryRow(
		`SELECT id, order_id, amount_cents, created_at FROM charges WHERE idempotency_key = ?`,
		req.IdempotencyKey).Scan(&got.ID, &got.OrderID, &got.AmountCents, &got.CreatedAt)
	if err != nil {
		return Charge{}, err
	}
	got.Replayed = true
	return got, nil
}

type RefundRequest struct {
	ChargeID       string `json:"charge_id"`
	OrderID        string `json:"order_id"`
	IdempotencyKey string `json:"idempotency_key,omitempty"`
}

func (s *Service) refund(w http.ResponseWriter, r *http.Request) {
	var req RefundRequest
	if err := labhttp.ReadJSON(r, &req); err != nil {
		labhttp.Fail(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	d := s.chaos.Take("refund")
	d.Wait()
	if d.Mode == chaos.BeforeCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "injected failure BEFORE the refund was written")
		return
	}

	var amount int64
	var orderID string
	err := s.db.QueryRow(`SELECT amount_cents, order_id FROM charges WHERE id = ?`, req.ChargeID).Scan(&amount, &orderID)
	if err == sql.ErrNoRows {
		labhttp.Fail(w, http.StatusNotFound, "unknown_charge", "no such charge: "+req.ChargeID)
		return
	} else if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}

	rf := Refund{ID: "ref_" + uuid.NewString()[:8], ChargeID: req.ChargeID, AmountCents: amount}
	var key any
	if req.IdempotencyKey != "" {
		key = req.IdempotencyKey
	}
	_, err = s.db.Exec(
		`INSERT INTO refunds (id, charge_id, order_id, amount_cents, idempotency_key, created_at) VALUES (?,?,?,?,?,?)`,
		rf.ID, rf.ChargeID, orderID, rf.AmountCents, key, time.Now().UTC().Format(time.RFC3339Nano))
	if store.IsUnique(err) {
		if err2 := s.db.QueryRow(`SELECT id, amount_cents FROM refunds WHERE idempotency_key = ?`,
			req.IdempotencyKey).Scan(&rf.ID, &rf.AmountCents); err2 != nil {
			labhttp.Fail(w, http.StatusInternalServerError, "db", err2.Error())
			return
		}
		rf.Replayed = true
	} else if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}

	if d.Mode == chaos.AfterCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "injected failure AFTER the refund was written")
		return
	}
	labhttp.WriteJSON(w, http.StatusOK, rf)
}

// ledger is the assertion surface. Scenarios do not eyeball charges; they ask
// what the customer's net position is, because that is the only number the
// customer would ever argue about.
func (s *Service) ledger(w http.ResponseWriter, r *http.Request) {
	orderID := r.URL.Query().Get("order_id")
	l := Ledger{OrderID: orderID}
	if err := s.db.QueryRow(
		`SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM charges WHERE order_id = ?`,
		orderID).Scan(&l.Charges, &l.ChargedCents); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	if err := s.db.QueryRow(
		`SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM refunds WHERE order_id = ?`,
		orderID).Scan(&l.Refunds, &l.RefundedCents); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	l.NetCents = l.ChargedCents - l.RefundedCents
	labhttp.WriteJSON(w, http.StatusOK, l)
}

func (s *Service) truncate(w http.ResponseWriter, r *http.Request) {
	s.db.Exec(`DELETE FROM charges`)
	s.db.Exec(`DELETE FROM refunds`)
	s.chaos.Reset()
	labhttp.WriteJSON(w, http.StatusOK, map[string]string{"status": "cleared"})
}
