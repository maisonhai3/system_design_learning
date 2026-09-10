# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]", "redis"]
# ///
"""Scenario 01 — invalidating before COMMIT poisons the cache permanently."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.cache import (  # noqa: E402
    read_cache_aside,
    user_key,
    write_del_after_commit,
    write_del_before_commit,
)
from lab.harness import Lab, Latch, redis_client, reset  # noqa: E402

USER = 1

lab = Lab(
    "01 — INVALIDATE BEFORE COMMIT",
    "Both transactions succeed. Nothing errors. The cache is wrong forever.",
)
reset()

W = lab.session("W")  # the writer: promoting Alice from guest to admin
R = lab.session("R")  # a reader that happens to arrive mid-write
rdb = redis_client()


# ---------------------------------------------------------------------------
lab.section("Warm the cache, so there is something to invalidate")

role = read_cache_aside(R, USER)
lab.note(f"Alice is {role!r} in Postgres and now also in Redis. Everything agrees.")


# ---------------------------------------------------------------------------
lab.section("The anomaly: DEL inside the transaction, COMMIT after")
lab.note(
    """
    The instinct is to invalidate as early as possible, to keep the window
    where cache and database disagree as small as you can. Watch what the
    window actually contains.
    """
)

latch = Latch()
writer = W.spawn(write_del_before_commit, W, USER, "admin", latch=latch)
latch.wait_for("invalidated")  # W is parked between its DEL and its COMMIT

# R is an ordinary GET /users/1. It knows nothing about W. Postgres MVCC means
# its SELECT is not blocked by W's row lock — it simply sees the pre-UPDATE row.
seen = read_cache_aside(R, USER)

latch.let_through("invalidated")
writer.wait()

db_role = W.scalar("SELECT role FROM users WHERE id = %s", (USER,))
cached = rdb.get(user_key(USER))

lab.broke(
    db_role == "admin" and cached == "guest",
    f"Postgres says {db_role!r}, Redis says {cached!r}",
    """
    Neither transaction failed. The reader got a 200. The writer got a 200.
    Nothing in your error rate, your logs or your traces records that the
    cache was just filled with a value the database had already replaced.
    """,
)

# The part that makes it an incident rather than a blip: nothing repairs it.
again = read_cache_aside(R, USER)
lab.broke(
    again == "guest",
    f"the next read is a cache HIT and still returns {again!r}",
    """
    A HIT never consults the database, so the wrong value is now
    self-sustaining. Without a TTL it survives until someone notices —
    which for a permission cache means until someone notices in production.
    """,
)

lab.note(
    """
    Why it happened, in one line: between the DEL and the COMMIT, Postgres
    still holds the OLD row. Any reader that misses in that window reads the
    old value and writes it back — AFTER the DEL that was meant to remove it.

    And the window is not microseconds. It is however long the rest of that
    transaction takes: the other four statements, the FK check, the lock wait
    on a hot row. Everything that makes a transaction slow makes this likelier.
    """
)


# ---------------------------------------------------------------------------
lab.section("The fix: COMMIT, then DEL")
lab.note(
    """
    Same interleaving, one line moved. The reader now arrives after the commit
    but before the invalidation — the worst moment for this ordering.
    """
)

reset()
read_cache_aside(R, USER)  # warm again

latch2 = Latch()
writer = W.spawn(write_del_after_commit, W, USER, "admin", latch=latch2)
latch2.wait_for("committed")

stale = read_cache_aside(R, USER)  # HIT on the old value — this is the cost
latch2.let_through("committed")
writer.wait()

after = read_cache_aside(R, USER)
cached = rdb.get(user_key(USER))

lab.held(
    after == "admin" and cached == "admin",
    f"the cache converged: Redis now holds {cached!r}",
    f"""
    One reader did see the stale {stale!r} — for the microseconds between
    COMMIT and DEL. That staleness is bounded and self-healing.
    Compare with the version above, which is unbounded and self-sustaining.
    A bounded inconsistency is a design decision. An unbounded one is an
    incident with your name on it.
    """,
)

lab.note(
    """
    Read that trade carefully, because it is the whole answer to the interview
    question. You do not get to eliminate the inconsistent window; you only get
    to choose whether it CLOSES BY ITSELF. Delete-after-commit closes. Delete-
    before-commit does not.
    """
)


# ---------------------------------------------------------------------------
lab.section("What this fix does NOT buy you")
lab.note(
    """
    Delete-after-commit is the right default, and it is not a proof. There is
    a second interleaving — reader starts BEFORE the commit and finishes AFTER
    the delete — that poisons the cache in exactly the same way, and no
    reordering of two statements can fix it.

    That is scenario 03. Do not skip it: it is the difference between reciting
    the standard answer and understanding why it is only a mitigation.
    """
)

lab.takeaway(
    """
    "Invalidate after the commit, never before. Before the commit the database
     still serves the old row, so any concurrent miss re-caches the value you
     just deleted — and because a cache hit never re-reads the database, that
     wrong value is self-sustaining. After the commit you still get a stale
     window, but it is bounded by the gap between COMMIT and DEL and it heals
     itself. I always pick the bounded one."
    """
)

rdb.close()
lab.finish()
