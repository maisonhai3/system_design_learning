# SSE through the gateway — the application is perfect, the page never loads

**The claim you are practising:** *"Everything that breaks SSE breaks in the
proxy, not in the application. The one that reaches production is compression,
because it only triggers for clients that send `Accept-Encoding` — so it passes
in curl and fails in every browser."*

## Measured on this machine

```
nginx /buffered/, Accept-Encoding: identity  →  first event after 0.02s
nginx /buffered/, Accept-Encoding: gzip      →  NO EVENT AT ALL (12s)
```

Same endpoint. Same server. Same second. The only difference is a request header
that curl omits by default and every browser sends.

## Why gzip kills a stream

`gzip on` for `text/*` is in every hardening guide, and `text/event-stream` **is**
`text/*`. The compressor works in blocks: it will not emit anything until it has
enough input to be worth compressing, so 200-byte SSE frames sit in the deflate
window while the user watches a spinner.

Nothing times out. Nothing errors. No log line is written. The request is open and
healthy and delivering nothing.

**Read the trigger again**, because it is why this reaches production: it is the
*client's* `Accept-Encoding`. Your curl test passes. Your integration test — if it
uses a plain HTTP client with compression off — passes. It fails only for real
browsers, which is to say only for users.

## Fix 1: the application says "do not transform this"

```
with X-Accel-Buffering: no  →  first event after 0.02s
```

No nginx change at all. Two response headers do this, and they belong on every
streaming endpoint you write:

| header | what it does |
|---|---|
| `X-Accel-Buffering: no` | nginx-specific; disables buffering (and with it the compression) for **this response** |
| `Cache-Control: no-cache, no-transform` | the standards-track one; tells any intermediary it may not re-encode the body |

Why send them when you also control the proxy config? **Because you often do
not.** The CDN, the corporate proxy, the ingress controller someone else manages,
the service mesh added next quarter — a response header travels with the response
and protects it in paths you have never seen.

## Fix 2: configure the proxy

```
nginx /streamed/ (buffering off)     →  0.04s
traefik (no buffering by default)    →  0.02s
```

Traefik needed no fix. That is a real difference and it is not the lesson — the
lesson is that "works out of the box" is a property of a *default*, and defaults
change, get overridden by a middleware someone adds, and differ between the proxy
you develop against and the one in front of production. Send the headers anyway.

## The second failure: an idle stream is an invisible stream

A feed is idle most of the time — that is the point of a push channel. To a
proxy, "healthy long-lived connection" and "leaked connection" look identical.

```
keep-alive interval:   10s
proxy_read_timeout:     5s
connection survived:   5.0s     ← closed, exactly on the timeout
```

The client reconnects — that is what `retry:` is for — so the symptom is not an
outage. It is a reconnect every five seconds, per user, forever: a login storm, an
authorization-service load spike, a decision cache thrashing, and a
"connections per second" graph nobody can explain.

Note the *shape* of this bug. It is a **relationship between two numbers that live
in two different repositories**, owned by two different teams: the application's
keep-alive interval and the proxy's idle timeout. Neither is wrong on its own.
Nothing validates them together, and nothing ever will. Write the relationship
down where both teams read it.

The fix is one line of output:

```
: keep-alive
```

A comment frame — a line starting with a colon. The SSE parser ignores it, so the
client never sees an event it must filter, and every proxy in the path sees
traffic and resets its idle timer.

Pick the interval as a fraction of the **shortest timeout in the path**, not of
the one you know about. A third to a half is usual, and the number you are
dividing is whichever proxy you forgot: the ingress, the CDN, the corporate MITM,
the cloud load balancer whose default is 60 seconds.

## The checklist

**On the response, from the application:**

```
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
: keep-alive        on every idle tick
retry: <ms>         so the reconnect interval is yours to choose
id: <cursor>        on every frame, or none of the resume design works
```

**On the proxy:**

- buffering off, compression off, caching off for this route
- read/idle timeout comfortably longer than the keep-alive interval
- response **write** timeout disabled — a response that never ends is the point
  (Traefik: `respondingTimeouts.writeTimeout: 0`)
- HTTP/1.1 to the upstream, and no `Connection: close`

And one thing that is neither: **a rate limiter counts requests, and one SSE
request is a thousand events.** Limiting streams needs a concurrency cap — which
is scenario 06.

## Run it

```bash
./lab.sh run 05

# by hand — watch the difference
curl -N --compressed -H "Authorization: Bearer $(./lab.sh token alice)" \
     "http://localhost:8092/buffered/feed?no_accel_header=true"
curl -N --compressed -H "Authorization: Bearer $(./lab.sh token alice)" \
     "http://localhost:8092/streamed/feed?no_accel_header=true"
# then, in another terminal:
./lab.sh publish 12
```

## The interview answer

> *"You built an SSE endpoint and it works locally but not through staging."*

"Almost always the proxy, and almost always compression. `gzip on` for `text/*`
matches `text/event-stream`, and the compressor holds small frames until it has a
block worth emitting — so nothing is delivered until the buffer fills. It only
triggers for clients that send `Accept-Encoding`, which is why it passes in curl
and fails in every browser.

I send `Cache-Control: no-transform` and `X-Accel-Buffering: no` from the endpoint
itself, because the response header protects the stream through proxies I don't
control. The second one is the idle timeout: a push channel is quiet by design and
looks like a leaked connection to a proxy, so I send a `: keep-alive` comment at a
fraction of the shortest timeout in the path. The real shape of that bug is that
it's a relationship between two numbers in two different repositories that nothing
validates together."
