// Package orders is the front door. It owns no business logic — it decides
// which orchestrator runs the saga, and it answers "where is my order?".
//
// The interesting part is that in temporal mode it has no database. Not "a
// small one": none. Start, query and signal all go to the Temporal server,
// which is already storing the only authoritative copy of how far the order
// got. Adding an orders table here would be adding a second copy of the truth,
// and the two would disagree the first time a write failed halfway.
package orders

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/client"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/naive"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
)

type Service struct {
	Temporal client.Client
	Naive    *naive.Orchestrator
}

type CreateRequest struct {
	Customer               string `json:"customer"`
	SKU                    string `json:"sku"`
	Qty                    int    `json:"qty"`
	AmountCents            int64  `json:"amount_cents"`
	Address                string `json:"address"`
	Mode                   string `json:"mode"`                               // "temporal" (default) | "naive"
	OrderID                string `json:"order_id,omitempty"`                 // supply one to test dedupe
	ApprovalTimeoutSeconds int    `json:"approval_timeout_seconds,omitempty"` // 0 -> 72h
	PackSeconds            int    `json:"pack_seconds,omitempty"`
	IdempotencyMode        string `json:"idempotency_mode,omitempty"` // stable | per-attempt | off
}

type CreateResponse struct {
	OrderID string `json:"order_id"`
	Mode    string `json:"mode"`
	RunID   string `json:"run_id,omitempty"`
	// Duplicate is true when this order id was already running. See the
	// comment on WorkflowIDReusePolicy below.
	Duplicate bool `json:"duplicate"`
}

type StatusResponse struct {
	saga.OrderState
	Mode string `json:"mode"`
	// Status is the Temporal execution status: RUNNING, COMPLETED, FAILED,
	// TIMED_OUT, CANCELED, TERMINATED. It is deliberately separate from Phase:
	// Phase is what the business thinks, Status is what the engine knows, and
	// a system that conflates them cannot tell "still working" from "gave up".
	Status string `json:"status,omitempty"`
}

func (s *Service) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.HandleFunc("POST /orders", s.create)
	mux.HandleFunc("GET /orders/{id}", s.status)
	mux.HandleFunc("POST /orders/{id}/approve", s.approve)
	return labhttp.Log("orders   ", mux)
}

func (s *Service) create(w http.ResponseWriter, r *http.Request) {
	var req CreateRequest
	if err := labhttp.ReadJSON(r, &req); err != nil {
		labhttp.Fail(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if req.SKU == "" {
		req.SKU = "WIDGET-1"
	}
	if req.Qty == 0 {
		req.Qty = 1
	}
	if req.Address == "" {
		req.Address = "1 Example Street"
	}
	if req.Mode == "" {
		req.Mode = "temporal"
	}
	orderID := req.OrderID
	if orderID == "" {
		orderID = "ord_" + uuid.NewString()[:8]
	}

	in := saga.OrderInput{
		OrderID: orderID, Customer: req.Customer, SKU: req.SKU, Qty: req.Qty,
		AmountCents: req.AmountCents, Address: req.Address,
		ApprovalTimeoutSeconds: req.ApprovalTimeoutSeconds, PackSeconds: req.PackSeconds,
		IdempotencyMode: saga.KeyMode(req.IdempotencyMode),
	}

	if req.Mode == "naive" {
		s.Naive.Start(in)
		labhttp.WriteJSON(w, http.StatusAccepted, CreateResponse{OrderID: orderID, Mode: "naive"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	run, err := s.Temporal.ExecuteWorkflow(ctx, client.StartWorkflowOptions{
		// The order id IS the workflow id. That one line buys deduplication of
		// the entire business process: a retried POST, a double-clicked button
		// or a redelivered queue message cannot start a second saga, because
		// the server refuses a second execution with the same id while one is
		// running. No dedupe table, no "have I seen this request" cache.
		ID:        orderID,
		TaskQueue: saga.TaskQueue,
		// Two different questions, two different knobs, and conflating them is
		// a common way to ship a dedupe bug:
		//
		//   ReusePolicy    what to do when a CLOSED execution has this id.
		//                  REJECT_DUPLICATE: once this order has run, it never
		//                  runs again, not even after it finished.
		//   ConflictPolicy what to do when a RUNNING execution has this id.
		//                  FAIL: the second submit is an error, not a takeover.
		//
		// Both are business decisions ("may a cancelled order be re-placed
		// under the same id?"), which is why neither has a safe default.
		WorkflowIDReusePolicy:    enums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
		WorkflowIDConflictPolicy: enums.WORKFLOW_ID_CONFLICT_POLICY_FAIL,
		// And this one is a trap worth knowing about. By DEFAULT the Go SDK
		// swallows WorkflowExecutionAlreadyStarted and hands back a handle to
		// the execution that is already running. The dedupe still works — one
		// saga, one charge — but ExecuteWorkflow returns no error, so this API
		// cannot tell a caller that their retry was a duplicate. Convenient,
		// and quietly lossy. Opt in to being told.
		WorkflowExecutionErrorWhenAlreadyStarted: true,
		// The saga's outer bound. Note this does NOT cover the approval wait in
		// production, where the gate is 72h — set it accordingly, or discover
		// that your workflows time out while a human is at lunch.
		WorkflowExecutionTimeout: 24 * time.Hour,
	}, saga.OrderWorkflow, in)

	if err != nil {
		var dup *serviceerror.WorkflowExecutionAlreadyStarted
		if errors.As(err, &dup) {
			labhttp.WriteJSON(w, http.StatusOK, CreateResponse{
				OrderID: orderID, Mode: "temporal", Duplicate: true})
			return
		}
		labhttp.Fail(w, http.StatusBadGateway, "temporal", err.Error())
		return
	}
	labhttp.WriteJSON(w, http.StatusAccepted, CreateResponse{
		OrderID: orderID, Mode: "temporal", RunID: run.GetRunID()})
}

func (s *Service) status(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")

	if st, ok := s.Naive.Get(id); ok {
		labhttp.WriteJSON(w, http.StatusOK, StatusResponse{OrderState: st, Mode: "naive"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	desc, err := s.Temporal.DescribeWorkflowExecution(ctx, id, "")
	if err != nil {
		var nf *serviceerror.NotFound
		if errors.As(err, &nf) {
			labhttp.Fail(w, http.StatusNotFound, "unknown_order", "no such order: "+id)
			return
		}
		labhttp.Fail(w, http.StatusBadGateway, "temporal", err.Error())
		return
	}
	// The enum stringifies as "Running"/"Completed" in some API versions and
	// as "WORKFLOW_EXECUTION_STATUS_RUNNING" in others. Normalise once, here,
	// rather than teaching every caller both spellings.
	status := strings.ToUpper(strings.TrimPrefix(
		desc.WorkflowExecutionInfo.Status.String(), "WORKFLOW_EXECUTION_STATUS_"))

	out := StatusResponse{Mode: "temporal", Status: status}
	out.OrderID = id

	// Query a running workflow for its state. This is a call INTO the live
	// process: it runs the query handler against replayed state, and it takes
	// no locks on anything a writer needs.
	if qv, qerr := s.Temporal.QueryWorkflow(ctx, id, "", saga.QueryState); qerr == nil {
		var st saga.OrderState
		if err := qv.Get(&st); err == nil {
			out.OrderState = st
		}
	} else if status != "RUNNING" {
		// A finished workflow cannot answer queries on some paths; fall back to
		// its return value, which history kept.
		var st saga.OrderState
		if err := s.Temporal.GetWorkflow(ctx, id, "").Get(ctx, &st); err == nil {
			out.OrderState = st
		} else {
			out.Failure = err.Error()
			if out.Phase == "" {
				out.Phase = saga.PhaseFailed
			}
		}
	}
	if out.Phase == "" {
		out.Phase = saga.Phase(status)
	}
	labhttp.WriteJSON(w, http.StatusOK, out)
}

func (s *Service) approve(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var sig saga.ApprovalSignal
	if r.ContentLength > 0 {
		if err := labhttp.ReadJSON(r, &sig); err != nil {
			labhttp.Fail(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
	} else {
		sig.Approved = true
	}

	if _, ok := s.Naive.Get(id); ok {
		if err := s.Naive.Approve(id, sig); err != nil {
			labhttp.Fail(w, http.StatusConflict, "no_waiter", err.Error())
			return
		}
		labhttp.WriteJSON(w, http.StatusOK, map[string]any{"signalled": id, "approved": sig.Approved})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	// A signal is durable and it is not a request to a process. It is appended
	// to the workflow's history by the server; whether a worker is running at
	// this instant is not the sender's problem. Restart every worker you have
	// and the approval is still there when they come back.
	if err := s.Temporal.SignalWorkflow(ctx, id, "", saga.SignalApproval, sig); err != nil {
		var nf *serviceerror.NotFound
		if errors.As(err, &nf) {
			labhttp.Fail(w, http.StatusNotFound, "unknown_order", "no such order: "+id)
			return
		}
		labhttp.Fail(w, http.StatusBadGateway, "temporal", err.Error())
		return
	}
	labhttp.WriteJSON(w, http.StatusOK, map[string]any{"signalled": id, "approved": sig.Approved})
}
