# The resume cursor — Alice walks into a lift

**The claim you are practising:** *"Pub/Sub delivers to whoever is connected at
that instant, so a client that reconnects cannot ask for what it missed — the
messages do not exist anywhere. Streams are an append-only log with monotonic
server-assigned ids, and that id is the SSE cursor."*

## Why Pub/Sub cannot do this

```
SUBSCRIBE feed
PUBLISH feed event-1     → delivered to 1 subscriber
*** the connection drops ***
PUBLISH feed event-2     → delivered to 0 subscribers
PUBLISH feed event-3     → delivered to 0 subscribers
```

Both of those publishes **succeeded**. There is no error, no backlog, no
dead-letter queue. The messages were handed to nobody and forgotten.

And that `delivered to N` is the closest Pub/Sub gets to a receipt — it is not
close. It counts *connections at that instant*, not consumers that will still be
alive when they try to act on the message. A subscriber that crashes inside its
handler was counted as delivered.

Pub/Sub is the right tool for plenty of things — a cache-invalidation notice, a
"config changed, reload" ping — where missing one is survivable because the next
one repairs it. A user's feed is not one of them.

## Why a log can

```
XADD feed * ...    → 1710000000123-0     the id is assigned by the server
XREAD ... STREAMS feed 1710000000123-0   → everything strictly after that
```

The id is doing all the work. It is monotonic, server-assigned,
`milliseconds-sequence`, and it is three things you would otherwise have to
build: an ordering, a deduplication key, and a resume point.

## The SSE half

```
id: 1710000000123-0
event: dataset.processed
data: {"dataset_id": 11, ...}
                                  ← the blank line is the dispatch
```

- The server writes `id:` on every frame.
- The browser stores it. On reconnect it sends `Last-Event-ID: <that id>` **by
  itself** — no application code involved.
- The server turns that header back into an `XREAD` position.

Three things that are easy to get wrong and silent when you do:

1. **No `id:` line, no resume.** The stream works perfectly until somebody's
   train enters a tunnel.
2. **The blank line is the dispatch.** A frame is not delivered until the parser
   sees `\n\n`. Forget the second newline and you get a stream that connects,
   transfers bytes, and never fires a single `onmessage`.
3. **Only native `EventSource` sets `Last-Event-ID` for you.** And `EventSource`
   cannot set an `Authorization` header — so if your auth is a bearer token you
   are using a fetch-based client, and *you* must send the header. This is the
   single most common way a "resumable" feed turns out not to be.

## The default that replays your entire history

The snippet everyone copies:

```python
last_event_id: str = Header(default="0-0")     # ← a bug with a long fuse
```

`0-0` means *from the beginning of the stream*. So every **fresh** connection —
every new tab, every reconnect after a deploy, every mobile app resuming from
background — replays the whole retained history. Measured in the lab:

```
fresh connection defaulting to 0-0:   4 events replayed
fresh connection defaulting to  $:    0 events replayed
```

A fresh connection and a resume are **different requests** and must not share a
default:

| | cursor |
|---|---|
| fresh connection | `$` — only what happens from now |
| resume | the `Last-Event-ID` the client actually sent |

It survives review because it looks like a feature at ten events and like an
outage at ten thousand.

## The resume window has an edge

`XADD ... MAXLEN ~ 1000` keeps a stream from growing forever — and that trim
silently defines how long a client may be away. Offline for more than MAXLEN
events and its cursor has been trimmed off the end.

**Redis will not tell you.** `XREAD` from a trimmed id returns the entries that
still exist, as if nothing were missing:

```
resuming from a trimmed cursor silently skips to seq=40    (40 events vanished)
```

The response looks exactly like a healthy one. The client believes it is caught
up. This is the failure described as *"the feed sometimes misses things"* and
never reproduced.

The server can detect it, because it knows the oldest id it still holds:

```python
oldest = (await redis.xrange(stream, count=1))[0][0]
if cursor < oldest:
    yield 'event: gap\ndata: {"from": "...", "oldest": "..."}\n\n'
```

Then the client re-fetches current state over a plain HTTP call instead of
quietly believing a partial stream. **Snapshot plus stream** is the standard
shape, and this is the signal that triggers the snapshot.

## Also worth setting

- **`retry: 3000`** — the server chooses the reconnect interval, not the
  browser. Jitter it: ten thousand clients dropped by a deploy will otherwise
  reconnect in the same millisecond.
- **`: keep-alive`** — a comment frame on every idle tick. Scenario 05 is about
  what happens without it.

## Run it

```bash
./lab.sh run 01

# by hand, in two terminals
./lab.sh sse alice
./lab.sh publish 12
```

## The interview answer

> *"The feed has to survive a mobile client reconnecting. How?"*

"Redis Streams, not Pub/Sub. Pub/Sub delivers to whoever is connected at that
instant, so anything published while the client was away doesn't exist anywhere
to be re-requested. A stream is an append-only log with a monotonic
server-assigned id, and that id is exactly what SSE needs: I emit it as `id:`,
the browser replays it as `Last-Event-ID`, and I turn it back into an `XREAD`
position.

Two details I'd get right. A fresh connection starts at `$`, not `0-0` — sharing
that default means every new tab replays the whole retained history. And because
`MAXLEN` trimming can drop a client's cursor, I compare it against the oldest
surviving id and send an explicit `gap` event so the client re-snapshots instead
of silently believing a partial feed."
