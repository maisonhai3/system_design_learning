# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "psycopg[binary]", "redis"]
# ///
"""Scenario 01 — the lift: what a reconnect costs, and what pays for it."""

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import (  # noqa: E402
    GATEWAY,
    Lab,
    SseSession,
    redis_client,
    reset,
    token_for,
)

lab = Lab(
    "01 — THE RESUME CURSOR",
    "Alice walks into a lift. Three events happen. What does she see on the way out?",
)
reset()

ALICE = token_for("alice")
rdb = redis_client()
CHANNEL = "feed:v1:pubsub:demo"
STREAM = "feed:v1:stream:demo"


# ---------------------------------------------------------------------------
lab.section("The anomaly: Pub/Sub has no yesterday")
lab.note(
    """
    Pub/Sub delivers to whoever is connected at the instant of PUBLISH. Not
    "connected recently". Not "will reconnect in three seconds". Connected, now.

    A subscriber, three messages, and a disconnection in the middle of them.
    """
)

received: list[str] = []
stop = threading.Event()


def subscriber():
    client = redis_client()
    pubsub = client.pubsub()
    pubsub.subscribe(CHANNEL)
    for message in pubsub.listen():
        if stop.is_set():
            break
        if message["type"] == "message":
            received.append(message["data"])
    pubsub.close()
    client.close()


thread = threading.Thread(target=subscriber, daemon=True)
thread.start()
time.sleep(0.4)  # let the SUBSCRIBE actually land

delivered = rdb.publish(CHANNEL, "event-1")
time.sleep(0.2)
print(f"     PUBLISH event-1 → delivered to {delivered} subscriber(s)")

stop.set()  # Alice steps into the lift
print("     *** Alice's connection drops ***")
time.sleep(0.3)

for msg in ("event-2", "event-3"):
    delivered = rdb.publish(CHANNEL, msg)
    print(f"     PUBLISH {msg} → delivered to {delivered} subscriber(s)")

time.sleep(0.3)
lab.broke(
    received == ["event-1"],
    f"Alice received {received} — events 2 and 3 do not exist anywhere",
    """
    Look at the delivery counts. Redis reported 0 subscribers for events 2 and
    3 and published them anyway, successfully. There is no error, no backlog,
    no dead-letter queue: the messages were handed to nobody and forgotten.

    Now notice the second, worse thing. That "delivered to N" number is the
    closest Pub/Sub gets to a receipt, and it is not close — it counts
    CONNECTIONS at that instant, not consumers who will still be alive when
    they try to act on the message. A subscriber that crashes mid-handler was
    counted as delivered.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Streams: the same three events, into a log")

for i in (1, 2, 3):
    entry_id = rdb.xadd(STREAM, {"seq": str(i), "body": f"event-{i}"})
    print(f"     XADD → {entry_id}")

first = rdb.xrange(STREAM, count=1)[0][0]
after_first = rdb.xread({STREAM: first}, count=10)[0][1]

lab.held(
    len(after_first) == 2 and after_first[0][1]["body"] == "event-2",
    f"XREAD from {first} returns exactly the two she missed",
    """
    The id is doing all the work. It is monotonic, server-assigned,
    `milliseconds-sequence`, and it is simultaneously three things you would
    otherwise have to build: an ordering, a deduplication key, and a resume
    point. "Give me everything after X" is the only question a reconnecting
    client can usefully ask, and a log is the only structure that can answer it.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The real thing: drop an SSE connection and reconnect")
lab.note(
    """
    Now over HTTP, through the gateway, with a browser's own resume mechanism.
    Watch the Last-Event-ID header on the second connection — the browser sets
    it from the `id:` line of the last frame it received, with no help from
    application code.
    """
)

reset()
publisher = lab.actor("admin", "alice")

first_leg = lab.stream("alice-1", f"{GATEWAY}/feed", token=ALICE).open()
time.sleep(0.8)  # let the stream establish and reach XREAD BLOCK

publisher.post("/events/12?mode=fanout")  # public benchmark dataset
frames = first_leg.wait_for(1, timeout=10, event="dataset.processed")
cursor = first_leg.last_event_id
first_leg.disconnect()

# Three events happen while she is in the lift.
for dataset in (10, 12, 10):
    publisher.post(f"/events/{dataset}?mode=fanout")

second_leg = lab.stream(
    "alice-2", f"{GATEWAY}/feed", token=ALICE, last_event_id=cursor
).open()
missed = second_leg.wait_for(3, timeout=10, event="dataset.processed")

lab.held(
    len(frames) == 1 and len(missed) == 3,
    f"reconnected at {cursor} and received all 3 events from the lift",
    """
    Nothing was replayed that she had already seen, and nothing was skipped.
    The client sent one header; the server turned it into an XREAD id.

    Two things in this that are easy to get wrong and invisible when you do:

      1. The server must emit `id:` on every frame. No id line, no
         Last-Event-ID, no resume — and the failure is silent, because the
         stream still works perfectly until someone's train enters a tunnel.
      2. Only the browser's native EventSource sets Last-Event-ID for you. A
         fetch-based client (and you will need one, because EventSource cannot
         send an Authorization header) has to send it itself.
    """,
)
second_leg.disconnect()


# ---------------------------------------------------------------------------
lab.section("The default that replays your entire history")
lab.note(
    """
    The textbook snippet reads:

        last_event_id: str = Header(default="0-0")

    That default is a bug with a very long fuse. "0-0" means "from the
    beginning of the stream", so every FRESH connection — every new tab, every
    reconnect after a deploy, every mobile app resuming from background —
    replays the entire retained history.
    """
)

fresh_from_zero = lab.stream(
    "bad", f"{GATEWAY}/feed", token=ALICE, last_event_id="0-0", trace=False
).open()
replayed = fresh_from_zero.wait_for(4, timeout=8, event="dataset.processed")
fresh_from_zero.disconnect()

fresh_default = lab.stream("good", f"{GATEWAY}/feed", token=ALICE, trace=False).open()
time.sleep(1.5)  # long enough to have received a backlog, if there were one
from_now = fresh_default.data_frames()
fresh_default.disconnect()

lab.measure("fresh connection defaulting to 0-0:", f"{len(replayed)} events replayed")
lab.measure("fresh connection defaulting to $:", f"{len(from_now)} events replayed")

lab.broke(
    len(replayed) >= 4 and len(from_now) == 0,
    "the 0-0 default replays history that the client did not ask for",
    """
    A fresh connection and a resume are DIFFERENT REQUESTS and must not share a
    default. Fresh means "$" — only what happens from now on. Resume means the
    cursor the client actually sent.

    The reason this survives code review is that it looks like a feature at ten
    events and like an outage at ten thousand: every reconnect ships the whole
    stream, so a deploy that reconnects every client at once multiplies your
    egress by the retention window.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The resume window has an edge, and the client must be told")
lab.note(
    """
    A stream is memory. `XADD ... MAXLEN ~ 1000` keeps it from growing forever,
    and that trim silently defines how long a client may be away: offline for
    more than MAXLEN events and the cursor has been trimmed off the end.

    Redis will not tell you. XREAD from a trimmed id returns the entries that
    still exist, as if nothing were missing.
    """
)

reset()
trimmed = "feed:v1:stream:trimmed"
for i in range(50):
    rdb.xadd(trimmed, {"seq": str(i)}, maxlen=10, approximate=False)

old_cursor = "1-1"  # a cursor from long before the surviving window
back = rdb.xread({trimmed: old_cursor}, count=100)[0][1]
oldest_surviving = rdb.xrange(trimmed, count=1)[0][0]

lab.broke(
    len(back) == 10 and back[0][1]["seq"] == "40",
    f"resuming from a trimmed cursor silently skips to seq={back[0][1]['seq']}",
    """
    Forty events vanished and the response looks exactly like a healthy one.
    The client believes it is caught up. This is the failure that gets
    described as "the feed sometimes misses things" and is never reproduced.
    """,
)

# The server can detect it, because it knows the oldest id it still holds.
def resume_is_safe(cursor: str) -> bool:
    return cursor >= oldest_surviving or cursor in ("$", "0-0")


lab.held(
    not resume_is_safe(old_cursor) and resume_is_safe(oldest_surviving),
    f"comparing the cursor against the oldest surviving id ({oldest_surviving}) detects the gap",
    """
    One XRANGE ... COUNT 1 tells you the oldest id you still hold. If the
    client's cursor is older than that, its resume is not a resume, and it
    deserves to be told so:

        event: gap
        data: {"from": "<their cursor>", "oldest": "<what survives>"}

    Then the client can re-fetch the current state over a plain HTTP call
    instead of quietly believing a partial stream. "Snapshot plus stream" is
    the standard shape, and this is the signal that triggers the snapshot.
    """,
)

lab.takeaway(
    """
    "Pub/Sub is fire-and-forget: it delivers to whoever is connected at that
     instant, so a client that reconnects has no way to ask for what it
     missed — the messages do not exist anywhere. Streams are an append-only
     log with monotonic server-assigned ids, and that id is the SSE cursor:
     the server sends it as `id:`, the browser replays it as Last-Event-ID,
     and the server turns it back into an XREAD position. Two details I would
     get right: a fresh connection defaults to `$`, not `0-0`, or every new
     tab replays the whole history; and because MAXLEN trimming can drop a
     client's cursor, I compare it against the oldest surviving id and send an
     explicit `gap` event so the client re-snapshots instead of silently
     believing a partial feed."
    """
)

rdb.close()
lab.finish()
