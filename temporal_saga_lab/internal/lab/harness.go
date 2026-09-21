// Package lab is the scenario harness.
//
// Every scenario is a program that states a claim, forces the situation that
// tests it, and asserts twice: that the anomaly actually reproduced, and that
// the fix actually held. It exits non-zero if either stops being true.
//
// The first assertion is the one people leave out, and it is the one that
// matters. A scenario that only checks the fix passes just as happily when the
// bug it was written for has quietly stopped happening — at which point it is
// no longer evidence of anything, it is decoration.
package lab

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/inventory"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/labhttp"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/orders"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/payments"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/saga"
	"github.com/maisonhai3/system_design_learning/temporal_saga_lab/internal/shipping"
)

const (
	bold  = "\033[1m"
	dim   = "\033[2m"
	red   = "\033[31m"
	green = "\033[32m"
	amber = "\033[33m"
	cyan  = "\033[36m"
	reset = "\033[0m"
)

type Run struct {
	name     string
	claim    string
	failures int
	started  time.Time
}

func NewRun(name, claim string) *Run {
	r := &Run{name: name, claim: claim, started: time.Now()}
	fmt.Printf("\n%s%s%s\n", bold, name, reset)
	fmt.Printf("%sclaim:%s %s\n\n", dim, reset, claim)
	return r
}

func (r *Run) Step(format string, a ...any) {
	fmt.Printf("%s▸%s %s\n", cyan, reset, fmt.Sprintf(format, a...))
}

func (r *Run) Note(format string, a ...any) {
	fmt.Printf("  %s%s%s\n", dim, fmt.Sprintf(format, a...), reset)
}

// Measure prints a number the scenario just observed. Numbers in a README age
// badly; numbers a runner prints are re-measured on your machine every time.
func (r *Run) Measure(label string, value any) {
	fmt.Printf("  %s%-42s%s %s%v%s\n", dim, label, reset, bold, value, reset)
}

// Anomaly asserts the broken behaviour reproduced.
func (r *Run) Anomaly(desc string, ok bool) { r.assert("ANOMALY", amber, desc, ok) }

// Fixed asserts the corrected behaviour held.
func (r *Run) Fixed(desc string, ok bool) { r.assert("FIXED  ", green, desc, ok) }

// Check is a plain assertion that is neither the bug nor the fix.
func (r *Run) Check(desc string, ok bool) { r.assert("CHECK  ", cyan, desc, ok) }

func (r *Run) assert(kind, color, desc string, ok bool) {
	if ok {
		fmt.Printf("  %s%s%s  %s\n", color, kind, reset, desc)
		return
	}
	r.failures++
	fmt.Printf("  %s%s%s  %s  %s<-- DID NOT HOLD%s\n", red, kind, reset, desc, red, reset)
}

func (r *Run) Fatalf(format string, a ...any) {
	fmt.Printf("\n%serror:%s %s\n", red, reset, fmt.Sprintf(format, a...))
	os.Exit(1)
}

func (r *Run) Must(err error, what string) {
	if err != nil {
		r.Fatalf("%s: %v", what, err)
	}
}

// Done prints the verdict and sets the exit code.
func (r *Run) Done() {
	el := time.Since(r.started).Round(100 * time.Millisecond)
	if r.failures == 0 {
		fmt.Printf("\n%s%s PASS %s %s (%s)\n\n", green, bold, reset, r.name, el)
		return
	}
	fmt.Printf("\n%s%s FAIL %s %s — %d assertion(s) did not hold (%s)\n\n",
		red, bold, reset, r.name, r.failures, el)
	os.Exit(1)
}

// ---------------------------------------------------------------------------

// runTag makes every order id in a run unique.
//
// It is here because of a rule the lab itself teaches: the order id IS the
// workflow id, and REJECT_DUPLICATE means an id is spent the moment it is used
// — for ever, not just while it runs. Re-running a scenario with hard-coded ids
// would hit the dedupe rule rather than the behaviour under test, and pass or
// fail depending on whether anyone had run it before. A guarantee you like is
// still a constraint you have to design around.
var runTag = strconv.FormatInt(time.Now().UnixNano()/1e6, 36)

// ID builds a readable, run-unique order id: ID("crash_naive") -> ord_crash_naive_l3k9fz
func ID(name string) string { return "ord_" + name + "_" + runTag }

// Lab is the set of services a scenario talks to.
type Lab struct {
	Orders    string
	Payments  string
	Inventory string
	Shipping  string
	HTTP      *labhttp.Client
	runtime   string // docker | local
}

func Open() *Lab {
	return &Lab{
		Orders:    labhttp.Env("LAB_ORDERS_URL", "http://localhost:8110"),
		Payments:  labhttp.Env("LAB_PAYMENTS_URL", "http://localhost:8111"),
		Inventory: labhttp.Env("LAB_INVENTORY_URL", "http://localhost:8112"),
		Shipping:  labhttp.Env("LAB_SHIPPING_URL", "http://localhost:8113"),
		HTTP:      labhttp.NewClient(20 * time.Second),
		runtime:   labhttp.Env("LAB_RUNTIME", "docker"),
	}
}

func (l *Lab) ctx() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 30*time.Second)
}

// Reset clears every service's database so a scenario starts from a known
// state. Scenarios that share a machine and do not do this produce results
// that depend on the order you ran them in, which is not evidence.
func (l *Lab) Reset() error {
	ctx, cancel := l.ctx()
	defer cancel()
	for _, base := range []string{l.Payments, l.Inventory, l.Shipping} {
		if err := l.HTTP.Do(ctx, "DELETE", base+"/_all", nil, nil); err != nil {
			return fmt.Errorf("reset %s: %w", base, err)
		}
	}
	return nil
}

// Chaos arms a failure on one service. times < 0 means "until reset".
func (l *Lab) Chaos(service, mode string, times int, delay time.Duration) error {
	return l.ChaosTarget(service, mode, times, delay, "")
}

// ChaosTarget arms a failure on one OPERATION of one service — "refund" but not
// "charge". Scenario 03 needs exactly that: a payments service that can take
// money and cannot give it back.
func (l *Lab) ChaosTarget(service, mode string, times int, delay time.Duration, op string) error {
	base := map[string]string{"payments": l.Payments, "inventory": l.Inventory, "shipping": l.Shipping}[service]
	if base == "" {
		return fmt.Errorf("unknown service %q", service)
	}
	ctx, cancel := l.ctx()
	defer cancel()
	return l.HTTP.Post(ctx, base+"/_chaos", map[string]any{
		"mode": mode, "times": times, "delay_ms": int(delay / time.Millisecond), "target": op,
	}, nil)
}

func (l *Lab) ClearChaos(service string) error {
	base := map[string]string{"payments": l.Payments, "inventory": l.Inventory, "shipping": l.Shipping}[service]
	ctx, cancel := l.ctx()
	defer cancel()
	return l.HTTP.Do(ctx, "DELETE", base+"/_chaos", nil, nil)
}

func (l *Lab) CreateOrder(req orders.CreateRequest) (orders.CreateResponse, error) {
	ctx, cancel := l.ctx()
	defer cancel()
	var out orders.CreateResponse
	err := l.HTTP.Post(ctx, l.Orders+"/orders", req, &out)
	return out, err
}

func (l *Lab) Status(orderID string) (orders.StatusResponse, error) {
	ctx, cancel := l.ctx()
	defer cancel()
	var out orders.StatusResponse
	err := l.HTTP.Get(ctx, l.Orders+"/orders/"+orderID, &out)
	return out, err
}

func (l *Lab) Approve(orderID string, approved bool) error {
	ctx, cancel := l.ctx()
	defer cancel()
	return l.HTTP.Post(ctx, l.Orders+"/orders/"+orderID+"/approve",
		saga.ApprovalSignal{Approved: approved, By: "scenario"}, nil)
}

func (l *Lab) Ledger(orderID string) (payments.Ledger, error) {
	ctx, cancel := l.ctx()
	defer cancel()
	var out payments.Ledger
	err := l.HTTP.Get(ctx, l.Payments+"/ledger?order_id="+orderID, &out)
	return out, err
}

func (l *Lab) Shipments(orderID string) ([]shipping.Shipment, error) {
	ctx, cancel := l.ctx()
	defer cancel()
	var out struct {
		Shipments []shipping.Shipment `json:"shipments"`
	}
	err := l.HTTP.Get(ctx, l.Shipping+"/shipments?order_id="+orderID, &out)
	return out.Shipments, err
}

func (l *Lab) Reservations(orderID string) ([]inventory.Reservation, error) {
	ctx, cancel := l.ctx()
	defer cancel()
	var out struct {
		Reservations []inventory.Reservation `json:"reservations"`
	}
	err := l.HTTP.Get(ctx, l.Inventory+"/reservations?order_id="+orderID, &out)
	return out.Reservations, err
}

func (l *Lab) Stock() (map[string]int, error) {
	ctx, cancel := l.ctx()
	defer cancel()
	var out struct {
		Stock map[string]int `json:"stock"`
	}
	err := l.HTTP.Get(ctx, l.Inventory+"/stock", &out)
	return out.Stock, err
}

// WaitForPhase polls until the order reaches one of the given phases.
// Returns the last status seen and whether it matched.
func (l *Lab) WaitForPhase(orderID string, timeout time.Duration, phases ...saga.Phase) (orders.StatusResponse, bool) {
	want := map[saga.Phase]bool{}
	for _, p := range phases {
		want[p] = true
	}
	deadline := time.Now().Add(timeout)
	var last orders.StatusResponse
	for time.Now().Before(deadline) {
		st, err := l.Status(orderID)
		if err == nil {
			last = st
			if want[st.Phase] {
				return st, true
			}
		}
		time.Sleep(150 * time.Millisecond)
	}
	return last, false
}

// Wait polls an arbitrary condition.
func Wait(timeout time.Duration, cond func() bool) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(150 * time.Millisecond)
	}
	return cond()
}

// ---------------------------------------------------------------------------
// Killing things.
//
// Scenario 01 needs a process to die abruptly — no graceful shutdown, no
// draining, no chance to write anything down. That is the failure mode every
// "we'll just handle it in a defer" design is built to survive and does not.

func (l *Lab) StopWorker() error  { return l.control("stop", "worker") }
func (l *Lab) StartWorker() error { return l.control("start", "worker") }

// StopOrders kills the front door, and with it the naive orchestrator's entire
// memory. There is no equivalent call for the Temporal side because there is
// nothing equivalent to lose.
func (l *Lab) StopOrders() error  { return l.control("stop", "orders") }
func (l *Lab) StartOrders() error { return l.control("start", "orders") }

// WaitHealthy blocks until a service answers /healthz again.
func (l *Lab) WaitHealthy(base string, timeout time.Duration) bool {
	return Wait(timeout, func() bool {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		return l.HTTP.Get(ctx, base+"/healthz", nil) == nil
	})
}

func (l *Lab) control(action, proc string) error {
	if l.runtime == "local" {
		return l.controlLocal(action, proc)
	}
	name := labhttp.Env("LAB_"+strings.ToUpper(proc)+"_CONTAINER", "tsl_"+proc)
	// `docker kill` sends SIGKILL. `docker stop` would send SIGTERM first and
	// give the process a chance to tidy up, which is precisely the courtesy a
	// crash does not extend.
	verb := map[string]string{"stop": "kill", "start": "start"}[action]
	out, err := exec.Command("docker", verb, name).CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker %s %s: %v: %s", verb, name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (l *Lab) controlLocal(action, proc string) error {
	dir := labhttp.Env("LAB_STATE_DIR", ".lab")
	pidFile := dir + "/" + proc + ".pid"
	switch action {
	case "stop":
		raw, err := os.ReadFile(pidFile)
		if err != nil {
			return fmt.Errorf("read %s: %w", pidFile, err)
		}
		pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
		if err != nil {
			return err
		}
		// SIGKILL: the point is that the process gets no say.
		return exec.Command("kill", "-9", strconv.Itoa(pid)).Run()
	case "start":
		bin := labhttp.Env("LAB_BIN_DIR", "./bin") + "/" + proc
		logf, err := os.OpenFile(dir+"/"+proc+".log", os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return err
		}
		cmd := exec.Command(bin)
		cmd.Env = os.Environ()
		cmd.Stdout, cmd.Stderr = logf, logf
		if err := cmd.Start(); err != nil {
			return err
		}
		return os.WriteFile(pidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0o644)
	}
	return fmt.Errorf("unknown action %q", action)
}
