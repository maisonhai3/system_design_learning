# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 01 — lost update: reproduce it, then prove three fixes."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, reset  # noqa: E402

COUNTER = "page_views"

lab = Lab(
    "01 — LOST UPDATE",
    "Two increments, one survivor. Both transactions commit successfully.",
)
reset()

A = lab.session("A")
B = lab.session("B")


# ---------------------------------------------------------------------------
lab.section("The anomaly: read-modify-write across two round trips")
lab.note(
    """
    Both sessions do what an ORM does by default: SELECT the row, add one in
    Python, UPDATE with the literal result.
    """
)

A.run("BEGIN")
a_read = A.scalar("SELECT value FROM counters WHERE name = %s", (COUNTER,))
B.run("BEGIN")
b_read = B.scalar("SELECT value FROM counters WHERE name = %s", (COUNTER,))

# Each session computes `read + 1` in application code — the bug is right here.
A.run("UPDATE counters SET value = %s WHERE name = %s", (a_read + 1, COUNTER))
A.run("COMMIT", note="succeeds")
B.run("UPDATE counters SET value = %s WHERE name = %s", (b_read + 1, COUNTER))
B.run("COMMIT", note="also succeeds — no error, no conflict")

final = A.scalar("SELECT value FROM counters WHERE name = %s", (COUNTER,))
lab.broke(
    final == 1,
    f"two +1s applied, counter reads {final} (expected 2)",
    "Neither transaction failed. Nothing in your error rate or your logs "
    "records that an update was destroyed.",
)


# ---------------------------------------------------------------------------
lab.section("Fix 1: do the arithmetic in the statement")
lab.note(
    """
    SET value = value + 1 is a single statement, so Postgres takes the row
    lock, reads, writes and releases it — with no window for anyone to slip in.
    Reach for this first, every time.
    """
)
reset()

A.run("BEGIN")
A.run("UPDATE counters SET value = value + 1 WHERE name = %s", (COUNTER,))

B.run("BEGIN")
pending = B.spawn("UPDATE counters SET value = value + 1 WHERE name = %s", (COUNTER,))

lab.held(
    pending.still_blocked_after(1.0),
    "B's UPDATE blocks on A's uncommitted row lock",
    "This is the queueing that makes the fix correct — and the same queueing "
    "that makes a hot row collapse under load. See scenario 07.",
)

A.run("COMMIT")
pending.wait()
B.run("COMMIT")

final = A.scalar("SELECT value FROM counters WHERE name = %s", (COUNTER,))
lab.held(
    final == 2,
    f"counter reads {final} after two +1s",
    "When B unblocked it re-read the row at READ COMMITTED and added 1 to "
    "A's new value, rather than to its stale snapshot.",
)


# ---------------------------------------------------------------------------
lab.section("Fix 2: SELECT ... FOR UPDATE, when app-side arithmetic is forced")
lab.note(
    """
    Sometimes the new value genuinely cannot be expressed in SQL. Then you take
    the row lock explicitly on the way in, and hold it across your round trip.
    """
)
reset()

A.run("BEGIN")
A.scalar("SELECT value FROM counters WHERE name = %s FOR UPDATE", (COUNTER,))

B.run("BEGIN")
pending = B.spawn("SELECT value FROM counters WHERE name = %s FOR UPDATE", (COUNTER,))
lab.held(
    pending.still_blocked_after(1.0),
    "B blocks on the SELECT, before it ever reaches its UPDATE",
    "A plain SELECT would not have blocked. FOR UPDATE is what makes readers "
    "queue — that is the point, and it is also the cost.",
)

A.run("UPDATE counters SET value = 10 WHERE name = %s", (COUNTER,))
A.run("COMMIT")

pending.wait()
b_sees = pending.rows[0][0]
lab.held(
    b_sees == 10,
    f"B's SELECT returns {b_sees} — A's committed value, not the stale one",
)
B.run("UPDATE counters SET value = %s WHERE name = %s", (b_sees + 1, COUNTER))
B.run("COMMIT")

final = A.scalar("SELECT value FROM counters WHERE name = %s", (COUNTER,))
lab.held(final == 11, f"counter reads {final}; no update was lost")


# ---------------------------------------------------------------------------
lab.section("Fix 3: optimistic locking with a version column")
lab.note(
    """
    No locks at all. The write carries the version it was based on; if that is
    no longer current, it matches zero rows. Note carefully: this raises NO
    error. Application code that does not check the row count has silently
    reinvented the lost update.
    """
)
reset()

a_val, a_ver = A.run(
    "SELECT value, version FROM counters WHERE name = %s", (COUNTER,)
)[0]
b_val, b_ver = B.run(
    "SELECT value, version FROM counters WHERE name = %s", (COUNTER,)
)[0]

A.run(
    """UPDATE counters SET value = value + 1, version = version + 1
       WHERE name = %s AND version = %s
       RETURNING value, version""",
    (COUNTER, a_ver),
    note="A wins",
)

stale = B.run(
    """UPDATE counters SET value = value + 1, version = version + 1
       WHERE name = %s AND version = %s
       RETURNING value, version""",
    (COUNTER, b_ver),
    note="B is stale",
)
lab.held(
    stale == [],
    "B's write matches zero rows instead of clobbering A",
    "Zero rows, not an exception. If your code does not check rowcount, this "
    "fix does nothing at all.",
)

# The retry the application would perform.
b_val, b_ver = B.run(
    "SELECT value, version FROM counters WHERE name = %s", (COUNTER,)
)[0]
retried = B.run(
    """UPDATE counters SET value = value + 1, version = version + 1
       WHERE name = %s AND version = %s
       RETURNING value, version""",
    (COUNTER, b_ver),
    note="retry with fresh version",
)
lab.held(
    retried[0][0] == 2,
    f"after retry the counter reads {retried[0][0]}; both increments survived",
)


lab.takeaway(
    """
    "Read-modify-write across two round trips is a lost update at READ COMMITTED.
     We moved the arithmetic into the statement so the database did the read and
     the write atomically. Where we had to compute in app code we took SELECT
     FOR UPDATE, and on the long editing screens we used a version column — but
     that one only works if you actually check the affected row count."
    """
)
lab.finish()
