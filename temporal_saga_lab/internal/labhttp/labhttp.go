// Package labhttp is the small amount of HTTP plumbing every service in this
// lab shares: JSON in, JSON out, and an error type that keeps the one
// distinction an orchestrator actually needs — "try again" vs "never again".
package labhttp

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// Error carries an HTTP status alongside the message. The status is not
// decoration: the orchestrators in this lab decide whether to retry by asking
// Retryable(), and getting that wrong in either direction is a real outage.
//
//	5xx / timeout / connection refused -> retry. The work may not have happened.
//	4xx                                -> do not retry. The answer will not change.
//
// Retrying a 409 "out of stock" until the retry budget runs out is the bug this
// type exists to prevent; it turns a two-second rejection into a two-minute one
// and still fails.
type Error struct {
	Status int
	Code   string
	Msg    string
}

func (e *Error) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("http %d (%s): %s", e.Status, e.Code, e.Msg)
	}
	return fmt.Sprintf("http %d: %s", e.Status, e.Msg)
}

// Retryable reports whether the caller may safely try this call again.
// Status 0 means the request never got an answer (timeout, refused, reset) —
// the most dangerous case, because the work may well have happened anyway.
// That ambiguity is the entire subject of scenario 02.
func (e *Error) Retryable() bool { return e.Status == 0 || e.Status >= 500 }

// Client is a JSON-over-HTTP client with a deadline. There is no retry loop in
// here on purpose: in this lab retries belong to the orchestrator, which is the
// layer that knows what has already been done and what must be undone.
type Client struct {
	HTTP *http.Client
}

func NewClient(timeout time.Duration) *Client {
	return &Client{HTTP: &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			DialContext:         (&net.Dialer{Timeout: 2 * time.Second}).DialContext,
			MaxIdleConnsPerHost: 32,
		},
	}}
}

func (c *Client) Do(ctx context.Context, method, url string, in, out any) error {
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return &Error{Status: 0, Msg: "encode request: " + err.Error()}
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return &Error{Status: 0, Msg: err.Error()}
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		// No status at all. The call may or may not have taken effect.
		return &Error{Status: 0, Msg: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode >= 400 {
		var p struct {
			Error string `json:"error"`
			Code  string `json:"code"`
		}
		_ = json.Unmarshal(raw, &p)
		if p.Error == "" {
			p.Error = string(bytes.TrimSpace(raw))
		}
		return &Error{Status: resp.StatusCode, Code: p.Code, Msg: p.Error}
	}
	if out != nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, out); err != nil {
			return &Error{Status: resp.StatusCode, Msg: "decode response: " + err.Error()}
		}
	}
	return nil
}

func (c *Client) Post(ctx context.Context, url string, in, out any) error {
	return c.Do(ctx, http.MethodPost, url, in, out)
}

func (c *Client) Get(ctx context.Context, url string, out any) error {
	return c.Do(ctx, http.MethodGet, url, nil, out)
}

// WriteJSON writes v as JSON with the given status.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

// Fail writes an error body in the shape Client.Do parses back.
func Fail(w http.ResponseWriter, status int, code, msg string) {
	WriteJSON(w, status, map[string]string{"error": msg, "code": code})
}

// ReadJSON decodes a request body, rejecting unknown fields so a typo in a
// scenario is a 400 instead of a silently ignored field.
func ReadJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

// Log is the one-line request log every service prints. Reading these three
// terminals side by side while a saga runs is most of what this lab is for.
func Log(service string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		rec := &statusRecorder{ResponseWriter: w, status: 200}
		start := time.Now()
		next.ServeHTTP(rec, r)
		fmt.Printf("%s  %-6s %-28s %d  %6.1fms\n",
			service, r.Method, r.URL.Path, rec.status, float64(time.Since(start).Microseconds())/1000)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// Flush lets the recorder sit in front of streaming handlers without breaking them.
func (s *statusRecorder) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Serve runs an HTTP server until ctx is cancelled, then shuts it down.
func Serve(ctx context.Context, addr string, h http.Handler) error {
	srv := &http.Server{Addr: addr, Handler: h}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()
	select {
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	case <-ctx.Done():
		shutCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		return srv.Shutdown(shutCtx)
	}
}
