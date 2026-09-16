// Package shipping is the step that fails last, which is what makes it useful.
//
// A saga is only interesting when something breaks AFTER you have already taken
// the customer's money. Shipping is the last step, so arming a failure here is
// the cheapest way to force a real rollback across two other services.
package shipping

import (
	"database/sql"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/chaos"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/store"
)

const DDL = `
CREATE TABLE IF NOT EXISTS shipments (
  id              TEXT PRIMARY KEY,
  order_id        TEXT NOT NULL,
  address         TEXT NOT NULL,
  state           TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_order ON shipments(order_id);
`

type Service struct {
	db    *store.DB
	chaos *chaos.Controller
}

func New(db *store.DB) *Service { return &Service{db: db, chaos: chaos.New()} }

type ShipRequest struct {
	OrderID        string `json:"order_id"`
	Address        string `json:"address"`
	IdempotencyKey string `json:"idempotency_key,omitempty"`
}

type Shipment struct {
	ID       string `json:"id"`
	OrderID  string `json:"order_id"`
	Address  string `json:"address"`
	State    string `json:"state"`
	Replayed bool   `json:"replayed"`
}

func (s *Service) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.Handle("/_chaos", s.chaos.Handler())
	mux.HandleFunc("POST /shipments", s.ship)
	mux.HandleFunc("GET /shipments", s.list)
	mux.HandleFunc("DELETE /_all", s.truncate)
	return labhttp.Log("shipping ", mux)
}

func (s *Service) ship(w http.ResponseWriter, r *http.Request) {
	var req ShipRequest
	if err := labhttp.ReadJSON(r, &req); err != nil {
		labhttp.Fail(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	d := s.chaos.Take("ship")
	d.Wait()
	if d.Mode == chaos.BeforeCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos", "carrier API is down")
		return
	}

	if req.IdempotencyKey != "" {
		var got Shipment
		err := s.db.QueryRow(`SELECT id, order_id, address, state FROM shipments WHERE idempotency_key = ?`,
			req.IdempotencyKey).Scan(&got.ID, &got.OrderID, &got.Address, &got.State)
		if err == nil {
			got.Replayed = true
			labhttp.WriteJSON(w, http.StatusOK, got)
			return
		} else if err != sql.ErrNoRows {
			labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
			return
		}
	}

	sh := Shipment{ID: "shp_" + uuid.NewString()[:8], OrderID: req.OrderID, Address: req.Address, State: "DISPATCHED"}
	var key any
	if req.IdempotencyKey != "" {
		key = req.IdempotencyKey
	}
	if _, err := s.db.Exec(
		`INSERT INTO shipments (id, order_id, address, state, idempotency_key, created_at) VALUES (?,?,?,?,?,?)`,
		sh.ID, sh.OrderID, sh.Address, sh.State, key, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}

	if d.Mode == chaos.AfterCommit {
		labhttp.Fail(w, http.StatusInternalServerError, "chaos",
			"carrier accepted the parcel, then the connection dropped (shipment "+sh.ID+" exists)")
		return
	}
	labhttp.WriteJSON(w, http.StatusOK, sh)
}

func (s *Service) list(w http.ResponseWriter, r *http.Request) {
	rows, err := s.db.Query(`SELECT id, order_id, address, state FROM shipments WHERE order_id = ? ORDER BY created_at`,
		r.URL.Query().Get("order_id"))
	if err != nil {
		labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
		return
	}
	defer rows.Close()
	out := []Shipment{}
	for rows.Next() {
		var sh Shipment
		if err := rows.Scan(&sh.ID, &sh.OrderID, &sh.Address, &sh.State); err != nil {
			labhttp.Fail(w, http.StatusInternalServerError, "db", err.Error())
			return
		}
		out = append(out, sh)
	}
	labhttp.WriteJSON(w, http.StatusOK, map[string]any{"shipments": out})
}

func (s *Service) truncate(w http.ResponseWriter, r *http.Request) {
	s.db.Exec(`DELETE FROM shipments`)
	s.chaos.Reset()
	labhttp.WriteJSON(w, http.StatusOK, map[string]string{"status": "cleared"})
}
