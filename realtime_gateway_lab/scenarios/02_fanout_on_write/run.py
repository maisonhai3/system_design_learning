# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "psycopg[binary]", "redis"]
# ///
"""Scenario 02 — one stream or ten thousand: where the filtering goes."""

import pathlib
import sys
import queue
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import GATEWAY, Lab, db, redis_client, reset, stream_contents  # noqa: E402

LISTENERS = 50
GLOBAL_STREAM = "feed:v1:stream:global"
PAYROLL = 11  # acme/payroll-q3-CONFIDENTIAL — restricted, Alice-only

lab = Lab(
    "02 — FAN-OUT ON WRITE",
    "The filtering has to happen somewhere. Where decides both your CPU bill and your blast radius.",
)
reset()

rdb = redis_client()


# ---------------------------------------------------------------------------
lab.section("The anomaly: one shared stream, and everybody does the filtering")
lab.note(
    f"""
    The design that writes itself: one `{GLOBAL_STREAM}`, every connection
    reading it, an `if` in the loop that drops what this subject may not see.

    It is easy to read, the policy lives in one place, and nobody has to think
    about fan-out. Then {LISTENERS} people connect and ONE event arrives for
    ONE of them.
    """
)

woken = 0
evaluated = 0
kept = 0
wait_seconds = 0.0
counters = threading.Lock()
ready = threading.Barrier(LISTENERS + 1)
stop = threading.Event()

# A bounded connection pool, because that is what a real service has, and
# because the herd's second cost only becomes visible against one. The
# database is configured with max_connections=30; a service that opened a
# connection per SSE listener would fall over at 30 users, which is scenario
# 06. Here the listeners share POOL_SIZE and queue for it.
POOL_SIZE = 10
pool: queue.Queue = queue.Queue()
for _ in range(POOL_SIZE):
    pool.put(db())


def shared_listener(user_id: int, org_id: int):
    """What every one of the N connections does when an event lands."""
    global woken, evaluated, kept, wait_seconds
    client = redis_client()
    cursor = "$"
    ready.wait()
    while not stop.is_set():
        result = client.xread({GLOBAL_STREAM: cursor}, count=10, block=500)
        if not result:
            continue
        for _stream, entries in result:
            for entry_id, fields in entries:
                cursor = entry_id
                with counters:
                    woken += 1
                # The ABAC check. It is not free: it is a query, per event, per
                # connection, and it is the same query N times over — against a
                # pool that N does not fit into.
                waited = time.perf_counter()
                conn = pool.get()
                queued_for = time.perf_counter() - waited
                try:
                    rows = conn.execute(
                        """
                        SELECT 1 FROM datasets d
                        WHERE d.id = %s AND d.org_id = %s
                          AND (d.classification IN ('public','internal')
                               OR EXISTS (SELECT 1 FROM dataset_grants g
                                          WHERE g.dataset_id = d.id AND g.user_id = %s))
                        """,
                        (int(fields["dataset_id"]), org_id, user_id),
                    ).fetchall()
                finally:
                    pool.put(conn)
                with counters:
                    evaluated += 1
                    wait_seconds += queued_for
                    if rows:
                        kept += 1
    client.close()


# One listener per connected user. Only Alice (1) may see the payroll dataset.
threads = [
    threading.Thread(target=shared_listener, args=(1 if i == 0 else 2, 100), daemon=True)
    for i in range(LISTENERS)
]
for t in threads:
    t.start()
ready.wait()
time.sleep(0.5)  # everyone parked in XREAD BLOCK

rdb.xadd(
    GLOBAL_STREAM,
    {
        "kind": "dataset.processed",
        "dataset_id": str(PAYROLL),
        "org_id": "100",
        "classification": "restricted",
        "dataset_name": "acme/payroll-q3-CONFIDENTIAL",
    },
)
time.sleep(1.5)
stop.set()
for t in threads:
    t.join(timeout=3)
while not pool.empty():
    pool.get().close()

lab.measure("connections listening:", str(LISTENERS))
lab.measure("connections woken by ONE event:", str(woken))
lab.measure("authorization checks run:", str(evaluated))
lab.measure("events actually delivered:", str(kept))
lab.measure(
    f"time spent queuing for a pool of {POOL_SIZE}:",
    f"{wait_seconds * 1000:.0f}ms across {evaluated} checks",
)

lab.broke(
    woken >= LISTENERS and kept == 1,
    f"{woken} connections woken and {evaluated} authorization checks run to deliver {kept} event",
    f"""
    {evaluated - kept} of those checks existed only to say no. That is the
    thundering herd, and it scales with your CONCURRENCY, not with your traffic:
    ten thousand idle users make one publish ten thousand times more expensive,
    and the graph you see is CPU rising while requests-per-second stays flat.

    Now the part that is not about performance at all. Read the listener again:
    to decide that Bob may not see the payroll event, Bob's connection had to
    RECEIVE the payroll event. Its dataset name was in his process's memory. It
    was one `logger.debug(fields)` away from your log aggregator, one exception
    handler away from Sentry, and one refactor away from being yielded.

    Your entire confidentiality guarantee is an `if` statement that four
    different people will edit this year.
    """,
)


# ---------------------------------------------------------------------------
lab.section("Fan-out on write: the event is never written where it may not go")
lab.note(
    """
    Move the decision to the write. One query answers "who may see this" — the
    policy run backwards — and the event is appended only to those mailboxes.

    Readers then have nothing to decide, which is the performance win. But look
    at the streams afterwards for the security one.
    """
)

reset()
publisher = lab.actor("admin", "alice")
result = publisher.json("POST", f"/events/{PAYROLL}?mode=fanout")

alice_stream = stream_contents("feed:v1:stream:user:1")
bob_stream = stream_contents("feed:v1:stream:user:2")

lab.measure("delivered to:", str(result["delivered_to"]))
lab.measure("withheld from:", str(result["withheld_from"]))
lab.measure("entries in Alice's stream:", str(len(alice_stream)))
lab.measure("entries in Bob's stream:", str(len(bob_stream)))

lab.held(
    result["delivered_to"] == [1] and len(bob_stream) == 0,
    "the restricted event exists in Alice's mailbox and nowhere else",
    """
    This is a stronger claim than "Bob's client filtered it out", and it is
    stronger in a way that survives a bad refactor: there is no code path that
    could leak the event to Bob, because there is no copy of it to leak. The
    guarantee moved from a runtime branch to the shape of the data.

    Note also what the API returned: `withheld_from` with a REASON per user.
    Recording why you did not deliver is what makes an authorization system
    debuggable a month later; "it just didn't show up" is not an investigation.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The cost nobody mentions: fan-out freezes the decision")
lab.note(
    """
    Fan-out evaluates the policy once, at write time, and then the mailbox IS
    the decision — a durable record of what somebody was allowed to see at one
    instant. Permissions do not hold still.
    """
)

# (a) Grant Bob access AFTER the event was published.
publisher.post(f"/grants/2/{PAYROLL}")
bob_after_grant = stream_contents("feed:v1:stream:user:2")

lab.broke(
    len(bob_after_grant) == 0,
    "Bob now has the grant, and the event is still not in his mailbox — and never will be",
    """
    Fan-out cannot deliver backwards. Every event published before a grant is
    invisible to the person who was just granted access, forever, and no amount
    of waiting fixes it. For a chat app nobody minds. For "you have been added
    to the payroll project, here is its history" it is a missing feature that
    looks like a bug and is actually an architecture.
    """,
)

# (b) Revoke Alice's access AFTER the event was delivered.
publisher.delete(f"/grants/1/{PAYROLL}")
alice_after_revoke = stream_contents("feed:v1:stream:user:1")

lab.broke(
    len(alice_after_revoke) == 1,
    "Alice's grant is revoked and the restricted event is still sitting in her mailbox",
    """
    Revocation does not un-deliver. The event, including the dataset's name,
    stays readable to her until the stream is trimmed past it — and she can
    reconnect with `Last-Event-ID: 0-0` and read it again tomorrow.

    Compare with redis_cache_lab's scenario 05, which is the same lesson on the
    cache side: "revoked at 14:02" means "loses access when the TTL expires".
    Here it means "loses access when MAXLEN trims it", which is a retention
    setting nobody chose with security in mind.
    """,
)


# ---------------------------------------------------------------------------
lab.section("The resolution: fan out pointers, authorize the dereference")
lab.note(
    """
    Both properties at once, and the trick is to notice they are properties of
    different things:

      the ROUTING may be decided at write time — that is the cheap part, and
        being slightly wrong about it costs a wasted mailbox entry;
      the CONTENT must be authorized at read time — that is the part where
        being wrong is a disclosure.

    So the mailbox holds an id, the body lives once under its own key, and the
    reader re-runs the policy when it exchanges one for the other.
    """
)

reset()
publisher.post(f"/events/{PAYROLL}?mode=pointer")
alice_ptr = stream_contents("feed:v1:stream:user:1")
bob_ptr = stream_contents("feed:v1:stream:user:2")

# Alice reads it, while she still holds the grant.
alice_feed = lab.stream(
    "alice", f"{GATEWAY}/feed?mode=pointer", token=publisher.token, last_event_id="0-0"
).open()
before_revoke = alice_feed.wait_for(1, timeout=8, event="dataset.processed")
alice_feed.disconnect()

# Now revoke, and let her reconnect and replay the very same mailbox entry.
publisher.delete(f"/grants/1/{PAYROLL}")
alice_again = lab.stream(
    "alice-2", f"{GATEWAY}/feed?mode=pointer", token=publisher.token, last_event_id="0-0"
).open()
time.sleep(2.0)
after_revoke = alice_again.data_frames()
alice_again.disconnect()

lab.measure("pointer entries in Alice's mailbox:", str(len(alice_ptr)))
lab.measure("pointer entries in Bob's mailbox:", str(len(bob_ptr)))
lab.measure("frames she received before the revoke:", str(len(before_revoke)))
lab.measure("frames she receives after the revoke:", str(len(after_revoke)))

lab.held(
    len(bob_ptr) == 0 and len(before_revoke) == 1 and len(after_revoke) == 0,
    "the pointer is still in her mailbox, and the body is no longer served",
    """
    Bob still never received a routing entry, so the herd is gone: he is not
    woken and runs no checks. Alice was woken, and the re-check at dereference
    caught up with the revoke.

    The honest costs, because an interviewer will ask for them:
      one extra round trip per event on the read path;
      a body whose retention must outlive every pointer that names it, or you
        serve gaps instead of events;
      and the policy now runs in two places, so it MUST be one function called
        twice — see app/usecases/authorize.py. A policy that exists twice is a
        policy that will be enforced once.
    """,
)


# ---------------------------------------------------------------------------
lab.section("What fan-out costs in storage, and when that stops being fine")

reset()
with db() as conn:
    conn.execute(
        """
        INSERT INTO users (id, email, display_name, role, org_id)
        SELECT 1000 + g, 'user' || g || '@acme.test', 'User ' || g, 'member', 100
        FROM generate_series(1, 200) g
        """
    )

full = publisher.json("POST", "/events/12?mode=fanout")   # public dataset: everyone
pointer = publisher.json("POST", "/events/12?mode=pointer")
audience = len(full["delivered_to"])

lab.measure("audience:", f"{audience} subscribers")
lab.measure("fan-out of the full body:", f"{full['bytes_written']:,} bytes")
lab.measure("fan-out of pointers:", f"{pointer['bytes_written']:,} bytes")
lab.measure(
    "at 10k subscribers, 1 KB events, 100 events/day:",
    f"{10_000 * 1024 * 100 / 1e9:.1f} GB/day copied  vs  {(10_000 * 20 + 1024) * 100 / 1e9:.3f} GB/day",
)

lab.held(
    pointer["bytes_written"] < full["bytes_written"],
    f"pointers wrote {full['bytes_written'] / pointer['bytes_written']:.1f}x less for the same audience",
    """
    Storage amplification is (event size x audience), and Redis Streams live in
    RAM. That is the number that decides whether fan-out on write is even
    available to you: a 1 KB event to 10,000 subscribers is 10 MB per event, and
    a stream with no MAXLEN keeps it.

    Which is why the honest answer to "fan-out on write or filter on read" is
    neither of them by reflex:

      small audience, cheap events, permissions stable   → fan out the body
      large audience, or permissions that change         → fan out pointers
      audience of one (a personal notification)          → it is the same thing
      genuinely public data                              → one shared stream is
                                                            correct, and there
                                                            is nothing to leak
    """,
)

lab.takeaway(
    """
    "One shared stream means every connected client is woken by every event and
     runs the authorization check itself — that scales with concurrency rather
     than traffic, so 50 listeners here turned one event into 50 checks to make
     one delivery. And it is not only a CPU argument: to decide Bob may not see
     the payroll event, Bob's process had to receive it, so confidentiality
     rests on an `if` statement rather than on the data's shape.

     So I fan out on write — one query answers who may see it, and the event is
     never written where it may not go. The cost people leave out is that
     fan-out FREEZES the decision: granting access later cannot deliver
     backwards, and revoking does not un-deliver. Where that matters I fan out
     ids rather than bodies and re-authorize at the dereference — routing at
     write time, content at read time — which also keeps the storage
     amplification down to a pointer per subscriber instead of a copy."
    """
)

rdb.close()
lab.finish()
