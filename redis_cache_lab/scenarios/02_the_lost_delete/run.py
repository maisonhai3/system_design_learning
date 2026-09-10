# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 02 — the DEL that never ran: TTL as a backstop, outbox as a fix."""

import pathlib
import sys
import time

import redis as redis_lib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.cache import read_cache_aside, user_key  # noqa: E402
from lab.harness import BOLD, Lab, redis_client, reset  # noqa: E402

USER = 1
DEAD_REDIS = "redis://localhost:6399/0"  # nothing is listening here, on purpose

lab = Lab(
    "02 — THE LOST DELETE",
    "BackgroundTasks lives in RAM. RAM does not survive a deploy.",
)
reset()

W = lab.session("W")
R = lab.session("R")
rdb = redis_client()


# ---------------------------------------------------------------------------
lab.section("Setup: a correct write path, with the invalidation lost")
lab.note(
    """
    The ordering from scenario 01 is now right: COMMIT, then DEL. The DEL is
    handed to BackgroundTasks so the client is not made to wait for it.

    Then the pod is evicted mid-deploy. The response was already sent; the
    background task never ran. We simulate that by simply not running it —
    which is exactly how faithful the simulation needs to be.
    """
)

read_cache_aside(R, USER, ttl=None)  # warm, with NO expiry set

W.sql("BEGIN")
W.sql("UPDATE users SET role = 'admin' WHERE id = %s", (USER,))
W.sql("COMMIT")
W.say("200 OK returned to the client; DEL queued on BackgroundTasks")
W.say("*** SIGKILL — pod evicted, in-memory task list gone ***")

ttl = R.cache_ttl(user_key(USER))
seen = read_cache_aside(R, USER, ttl=None)

lab.broke(
    seen == "guest" and ttl == -1,
    f"cache still serves {seen!r} and TTL is {ttl} (-1 means: no expiry, ever)",
    """
    The data change is durable — it is in Postgres. Its consequence was not.
    That asymmetry is the entire bug: you made one system's write durable and
    left the other system's write in a Python list.
    """,
)

# The arithmetic that turns "stale" into an incident report.
for rps in (10, 200):
    lab.measure(
        f"at {rps} req/s, wrong answers served before someone notices:",
        f"{rps * 60:,} in the first minute, {rps * 3600:,} in the first hour",
    )


# ---------------------------------------------------------------------------
lab.section("Backstop 1: a TTL turns 'forever' into a number you chose")
lab.note(
    """
    A TTL is not a memory-management setting. It is the maximum time your
    system is allowed to be wrong when everything else fails — and for a
    permission cache, that is a security parameter with a number attached.
    """
)

reset()
read_cache_aside(R, USER, ttl=2)  # a 2-second TTL so the lab can wait for it

W.sql("BEGIN")
W.sql("UPDATE users SET role = 'admin' WHERE id = %s", (USER,))
W.sql("COMMIT")
W.say("*** the DEL is lost again ***")

stale = read_cache_aside(R, USER, ttl=2)
R.say("waiting out the TTL rather than fixing anything…")
time.sleep(2.2)
healed = read_cache_aside(R, USER, ttl=2)

lab.held(
    stale == "guest" and healed == "admin",
    "the entry expired on its own and the next read repaired it",
    """
    Nobody did anything. No alert, no runbook, no redeploy. That is the only
    property a backstop needs: it must not require a human.
    """,
)

lab.note(
    """
    Now choose the number honestly, because the choice IS the design:

      TTL 24h    great hit rate, and a revoked admin keeps their access for a day
      TTL 5m     the usual default: 5 minutes of wrongness in the worst case
      TTL 30s    a permission cache you can defend in a security review
      TTL 0      no cache. Sometimes the correct answer, and always the
                 baseline you should measure the others against.

    "What is your TTL" is really "how long may this system be wrong". If you
    cannot answer the second question, you were never entitled to the first.
    """
)


# ---------------------------------------------------------------------------
lab.section("Why the DEL gets lost in the first place — two different ways")

reset()
read_cache_aside(R, USER, ttl=300)

W.sql("BEGIN")
W.sql("UPDATE users SET role = 'admin' WHERE id = %s", (USER,))
W.sql("COMMIT")

# Failure mode 2: Redis is up, your process is up, the network is not.
dead = redis_lib.Redis.from_url(DEAD_REDIS, socket_connect_timeout=1)
try:
    dead.delete(user_key(USER))
    lost_to_error = False
except redis_lib.RedisError as exc:
    lost_to_error = True
    W.log(f"DEL {user_key(USER)}", f"✗ {type(exc).__name__} — Redis unreachable")

still_stale = rdb.get(user_key(USER))
lab.broke(
    lost_to_error and still_stale == "guest",
    "the DEL raised, the client had already been sent its 200, nobody retried",
    """
    A background task that raises is logged by the server and invisible to the
    caller — the response left the building milliseconds ago. So this failure
    does not appear in your API error rate at all. It appears, weeks later, as
    "a user says they still see the old permissions" with nothing in the logs
    to connect it to.
    """,
)
dead.close()


# ---------------------------------------------------------------------------
lab.section("Backstop 2: make the invalidation as durable as the write")
lab.note(
    """
    You cannot make Postgres and Redis atomic. What you can do is record the
    INTENT to invalidate in the same transaction as the data change, so the two
    commit or fail together, and let a separate relay carry out the intent —
    retrying until Redis accepts it.

    That is the transactional outbox. Same pattern this repo's
    student_course_enrollment lab uses to publish messages, for the same reason.
    """
)

reset()
read_cache_aside(R, USER, ttl=300)

W.sql("BEGIN")
W.sql("UPDATE users SET role = 'admin' WHERE id = %s", (USER,))
W.sql(
    "INSERT INTO cache_invalidations (cache_key) VALUES (%s)",
    (user_key(USER),),
    note="same transaction as the data change",
)
W.sql("COMMIT")
W.say("*** SIGKILL again — the process dies before anything touches Redis ***")

survived = W.scalar(
    "SELECT count(*) FROM cache_invalidations WHERE drained_at IS NULL"
)

# A new process — the relay — starts up minutes later and drains the backlog.
RELAY = lab.session("RLY")
RELAY.say("relay process starting; draining the outbox")
pending = RELAY.sql(
    """
    SELECT id, cache_key FROM cache_invalidations
    WHERE drained_at IS NULL ORDER BY id
    FOR UPDATE SKIP LOCKED
    """,
    note="SKIP LOCKED so N relay replicas never fight over the same row",
)
for row_id, key in pending:
    RELAY.cache_del(key)
    RELAY.sql(
        "UPDATE cache_invalidations SET drained_at = now() WHERE id = %s", (row_id,)
    )

recovered = read_cache_aside(R, USER, ttl=300)
lab.held(
    survived == 1 and recovered == "admin",
    "the invalidation survived the crash because it was committed with the data",
    """
    Note what this does NOT give you: it is not faster, and it is not
    synchronous. The cache is stale for as long as the relay lag. What it gives
    you is that the staleness is bounded by a lag you can MEASURE and alarm on,
    instead of by a TTL you guessed.
    """,
)

lab.note(
    """
    The honest cost, since an interviewer will ask: one more table, one more
    process to run and monitor, one more backlog that can grow. For a profile
    cache that is over-engineering — use delete-after-commit plus a TTL. For a
    permission cache in a system where "still has access" is the failure mode,
    it is the difference between a bounded risk and an unbounded one.

    The other production-grade options, briefly:
      LISTEN/NOTIFY   no extra table, but delivery is at-most-once — a
                      disconnected listener misses notifications silently.
      CDC (Debezium)  reads the WAL, so it cannot be bypassed by any writer,
                      including psql at 2am. Heaviest to operate.
      Redis TTL only  no moving parts at all. Fine, once you have said out loud
                      how long you are willing to be wrong.
    """
)

lab.takeaway(
    """
    "BackgroundTasks is in-process and in-memory: if the pod dies between the
     commit and the DEL, the invalidation is gone while the write is durable.
     So I always set a TTL — it is the bound on how long the system may be
     wrong when the invalidation is lost, and for a permission cache I treat
     that number as a security parameter. If losing an invalidation is not
     acceptable at all, I write the invalidation into the same transaction as
     the data change and drain it with a relay, so it is exactly as durable as
     the write it belongs to."
    """
)

rdb.close()
lab.finish()
