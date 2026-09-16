// Package inventory is the service that is allowed to say no.
//
// Payments and shipping fail because something broke. Inventory fails because
// the answer is "there are four left and you asked for ten" — a decision, not
// a fault. Telling those two apart is the orchestrator's job, and this service
// exists to make sure it has to.
package inventory

import (
	"database/sql"
	"net/http"
	"strconv"
	"time"

	"github.com/google/uuid"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/chaos"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/store"
)

const DDL = `
CREATE TABLE IF NOT EXISTS stock (
  sku     TEXT PRIMARY KEY,
  on_hand INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
  id              TEXT PRIMARY KEY,
  order_id        TEXT NOT NULL,
  sku             TEXT NOT NULL,
  qty             INTEGER NOT NULL,
  state           TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_order ON reservations(order_id);
INSERT OR IGNORE INTO stock (sku, on_hand) VALUES ('WIDGET-1', 100), ('SCARCE-1', 1);
`

type Service struct {
	db    *store.DB
	chaos *chaos.Controller
}

func New(db *store.DB) *Service { return &Service{db: db, chaos: chaos.New()} }

type ReserveRequest struct {
	OrderID        string `json:"order_id"`
	SKU            string `json:"sku"`
	Qty            int    `json:"qty"`
	IdempotencyKey string `json:"idempotency_key,omitempty"`
}

type Reservation struct {
	ID       string `json:"id"`
	OrderID  string `json:"order_id"`
	SKU      string `json:"sku"`
	Qty      int    `json:"qty"`
	State    string `json:"state"`
	Replayed bool   `json:"replayed"`
}

func (s *Service) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.Handle("/_chaos", s.chaos.Handler())
	mux.HandleFunc("POST /reservations", s.reserve)
	mux.HandleFunc("POST /reservations/{id}/release", s.release)
	mux.HandleFunc("GET /reservations", s.list)
	mux.HandleFunc("GET /stock", s.stock)
	mux.HandleFunc("DELETE /_all", s.truncate)
	return labhttp.Log("inventory", mux)
}

func (s *Service) reserve(w http.ResponseWriter, r *http.Request) {
	var req ReserveRequest
	if err := labhttp.ReadJSON(r, &req); err != nil {
		labhttp.Fail(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	d := s.chaos.Take("reserve")
	d.Wait()
	if d.Mode == chaos.BeforeCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "injected failure BEFORE the reservation was written")
		return
	}

	if req.IdempotencyKey != "" {
		var got Reservation
		err := s.db.QueryRow(
			`SELECT id, order_id, sku, qty, state FROM reservations WHERE idempotency_key = ?`,
			req.IdempotencyKey).Scan(&got.ID, &got.OrderID, &got.SKU, &got.Qty, &got.State)
		if err == nil {
			got.Replayed = true
			labhttp.WriteJSON(w, http.StatusOK, got)
			return
		} else if err != sql.ErrNoRows {
			labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
			return
		}
	}

	var onHand int
	err := s.db.QueryRow(`SELECT on_hand FROM stock WHERE sku = ?`, req.SKU).Scan(&onHand)
	if err == sql.ErrNoRows {
		labhttp.Fail(w, http.StatusNotFound, "unknown_sku", "no such sku: "+req.SKU)
		return
	} else if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	if onHand < req.Qty {
		// 409, not 500. The distinction matters more than it looks: an
		// orchestrator that retries this will burn its whole retry budget
		// re-asking a question whose answer nobody is going to change, and
		// then fail anyway — slower, and with a misleading error.
		labhttp.Fail(w, http.StatusConflict, "insufficient_stock",
			"only "+strconv.Itoa(onHand)+" left of "+req.SKU)
		return
	}

	res := Reservation{
		ID: "res_" + uuid.NewString()[:8], OrderID: req.OrderID,
		SKU: req.SKU, Qty: req.Qty, State: "HELD",
	}
	var key any
	if req.IdempotencyKey != "" {
		key = req.IdempotencyKey
	}
	if _, err := s.db.Exec(
		`INSERT INTO reservations (id, order_id, sku, qty, state, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?)`,
		res.ID, res.OrderID, res.SKU, res.Qty, res.State, key, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	if _, err := s.db.Exec(`UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?`, req.Qty, req.SKU); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}

	if d.Mode == chaos.AfterCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos",
			"injected failure AFTER the reservation was written (reservation "+res.ID+" holds stock)")
		return
	}
	labhttp.WriteJSON(w, http.StatusOK, res)
}

// release is the compensation for reserve. It is idempotent by state rather
// than by key: releasing an already-released reservation is a no-op that
// returns 200.
//
// That is the right shape for a compensation. Compensations run on the worst
// day, from a retry loop, possibly twice — so "already done" must mean success,
// not an error that sends the rollback down a second failure path.
func (s *Service) release(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	d := s.chaos.Take("release")
	d.Wait()
	if d.Mode == chaos.BeforeCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "injected failure BEFORE the release was written")
		return
	}

	var res Reservation
	err := s.db.QueryRow(`SELECT id, order_id, sku, qty, state FROM reservations WHERE id = ?`, id).
		Scan(&res.ID, &res.OrderID, &res.SKU, &res.Qty, &res.State)
	if err == sql.ErrNoRows {
		labhttp.Fail(w, http.StatusNotFound, "unknown_reservation", "no such reservation: "+id)
		return
	} else if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	if res.State == "RELEASED" {
		res.Replayed = true
		labhttp.WriteJSON(w, http.StatusOK, res)
		return
	}

	if _, err := s.db.Exec(`UPDATE reservations SET state = 'RELEASED' WHERE id = ?`, id); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	if _, err := s.db.Exec(`UPDATE stock SET on_hand = on_hand + ? WHERE sku = ?`, res.Qty, res.SKU); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	res.State = "RELEASED"

	if d.Mode == chaos.AfterCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "injected failure AFTER the release was written")
		return
	}
	labhttp.WriteJSON(w, http.StatusOK, res)
}

func (s *Service) list(w http.ResponseWriter, r *http.Request) {
	rows, err := s.db.Query(
		`SELECT id, order_id, sku, qty, state FROM reservations WHERE order_id = ? ORDER BY created_at`,
		r.URL.Query().Get("order_id"))
	if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	defer rows.Close()
	out := []Reservation{}
	for rows.Next() {
		var res Reservation
		if err := rows.Scan(&res.ID, &res.OrderID, &res.SKU, &res.Qty, &res.State); err != nil {
			labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
			return
		}
		out = append(out, res)
	}
	labhttp.WriteJSON(w, http.StatusOK, map[string]any{"reservations": out})
}

func (s *Service) stock(w http.ResponseWriter, r *http.Request) {
	rows, err := s.db.Query(`SELECT sku, on_hand FROM stock ORDER BY sku`)
	if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var sku string
		var n int
		if err := rows.Scan(&sku, &n); err != nil {
			labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
			return
		}
		out[sku] = n
	}
	labhttp.WriteJSON(w, http.StatusOK, map[string]any{"stock": out})
}

func (s *Service) truncate(w http.ResponseWriter, r *http.Request) {
	s.db.Exec(`DELETE FROM reservations`)
	s.db.Exec(`UPDATE stock SET on_hand = 100 WHERE sku = 'WIDGET-1'`)
	s.db.Exec(`UPDATE stock SET on_hand = 1 WHERE sku = 'SCARCE-1'`)
	s.chaos.Reset()
	labhttp.WriteJSON(w, http.StatusOK, map[string]string{"status": "cleared"})
}
