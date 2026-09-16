// Package chaos is the failure injector every service in this lab embeds.
//
// The modes are deliberately named after WHERE the failure lands relative to
// the write, because that is the only thing the caller cannot see and the only
// thing that decides whether a retry is safe:
//
//	before_commit  the write did not happen. A retry is free.
//	after_commit   the write DID happen, and the caller was told it failed.
//	               A retry duplicates it — unless the caller brought a key.
//	slow           the write happens, eventually. Whether that counts as a
//	               failure is decided by someone else's timeout.
//
// "after_commit" is not an exotic fault. It is what every crashed process,
// dropped connection and overloaded load balancer looks like from outside.
// A system that only survives "before_commit" has not been tested.
package chaos

import (
	"encoding/json"
	"net/http"
	"sync"
	"time"
)

type Mode string

const (
	Off          Mode = "off"
	BeforeCommit Mode = "before_commit"
	AfterCommit  Mode = "after_commit"
	Slow         Mode = "slow"
)

// Directive is what one request gets. Taken once, at the top of a handler, so
// the handler reads as a straight line and you can see exactly which side of
// the write the injected failure sits on.
type Directive struct {
	Mode  Mode
	Delay time.Duration
}

type Controller struct {
	mu     sync.Mutex
	mode   Mode
	times  int
	delay  time.Duration
	target string
}

func New() *Controller { return &Controller{mode: Off} }

func (c *Controller) Set(mode Mode, times int, delay time.Duration, target string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.mode, c.times, c.delay, c.target = mode, times, delay, target
}

func (c *Controller) Reset() { c.Set(Off, 0, 0, "") }

// Take consumes one unit of the configured budget for the named operation.
// times < 0 means "until reset", which is how a permanent outage is spelled.
//
// The operation name matters more than it looks. Scenario 03 needs a payments
// service whose refund endpoint is broken while its charge endpoint works,
// because "the rollback failed" is a different — and much worse — situation
// than "the payment failed", and a fault injector that can only break a whole
// service cannot tell you which one you survive.
func (c *Controller) Take(op string) Directive {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.mode == Off || c.times == 0 {
		return Directive{Mode: Off}
	}
	if c.target != "" && c.target != op {
		return Directive{Mode: Off}
	}
	if c.times > 0 {
		c.times--
	}
	return Directive{Mode: c.mode, Delay: c.delay}
}

// Wait applies the Slow mode's delay. Separate from Take so a handler can log
// what it is about to do before it stalls.
func (d Directive) Wait() {
	if d.Mode == Slow && d.Delay > 0 {
		time.Sleep(d.Delay)
	}
}

type state struct {
	Mode    Mode   `json:"mode"`
	Times   int    `json:"times"`
	DelayMs int    `json:"delay_ms"`
	Target  string `json:"target,omitempty"` // operation name; empty means "any"
}

// Handler exposes the controller at /_chaos so scenarios can arm a failure
// from the outside instead of the service deciding to misbehave on a timer.
// A fault you cannot aim is a fault you cannot write an assertion about.
func (c *Controller) Handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			c.mu.Lock()
			s := state{Mode: c.mode, Times: c.times, DelayMs: int(c.delay / time.Millisecond), Target: c.target}
			c.mu.Unlock()
			writeJSON(w, http.StatusOK, s)
		case http.MethodDelete:
			c.Reset()
			writeJSON(w, http.StatusOK, state{Mode: Off})
		case http.MethodPost:
			var s state
			if err := json.NewDecoder(r.Body).Decode(&s); err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
				return
			}
			if s.Times == 0 {
				s.Times = 1
			}
			c.Set(s.Mode, s.Times, time.Duration(s.DelayMs)*time.Millisecond, s.Target)
			writeJSON(w, http.StatusOK, s)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
