# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 04 — deadlock: build the cycle, then remove it with lock ordering."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, reset  # noqa: E402

lab = Lab(
    "04 — DEADLOCK",
    "Two transfers in opposite directions. Postgres kills one; you do not choose which.",
)
reset()

A = lab.session("A")
B = lab.session("B")


# ---------------------------------------------------------------------------
lab.section("The anomaly: two transactions, two locks, opposite orders")
lab.note(
    """
    A sends money to bob (debits alice first).
    B sends money to alice (debits bob first).
    Each one's second statement wants the row the other is holding.
    """
)

A.run("BEGIN")
A.run("UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'alice'")
B.run("BEGIN")
B.run("UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'bob'")

# Both halves of the cycle are issued concurrently; whichever backend's lock
# wait trips deadlock_timeout first runs the detector and picks the victim.
pa = A.spawn("UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'bob'")
pb = B.spawn("UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'alice'")

pa.wait(timeout=20)
pb.wait(timeout=20)

victims = [p for p in (pa, pb) if p.error is not None]
codes = [p.error.sqlstate for p in victims]
lab.broke(
    len(victims) == 1 and codes == ["40P01"],
    f"exactly one session aborted with {codes or 'nothing'} (deadlock_detected)",
    f"The victim was session {victims[0].session.name if victims else '?'} — "
    "chosen by whichever backend detected the cycle, not by you. Every "
    "transaction on a deadlock-prone path needs its own retry.",
)

for p in (pa, pb):
    (p.session.run("ROLLBACK") if p.error else p.session.run("COMMIT"))

total = A.scalar("SELECT sum(balance_cents) FROM accounts")
lab.held(
    total == 200000,
    f"money is conserved: total is {total}",
    "The aborted transaction rolled back cleanly. A deadlock is a failed "
    "transaction, not a corrupt one.",
)


# ---------------------------------------------------------------------------
lab.section("Fix: acquire locks in a deterministic order")
lab.note(
    """
    Both transactions lock BOTH rows up front, sorted by primary key. Identical
    order means the second one queues instead of forming a cycle.
    """
)
reset()

ORDERED_LOCK = """SELECT id FROM accounts WHERE owner IN ('alice','bob')
                  ORDER BY id FOR UPDATE"""

A.run("BEGIN")
A.run(ORDERED_LOCK, note="A holds both")

B.run("BEGIN")
pb = B.spawn(ORDERED_LOCK)
lab.held(
    pb.still_blocked_after(1.5),
    "B waits — and keeps waiting well past the 500ms deadlock_timeout",
    "If this were a cycle, Postgres would have killed someone by now. An "
    "ordinary wait can last as long as it likes.",
)

A.run("UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'alice'")
A.run("UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'bob'")
A.run("COMMIT")

pb.wait()
lab.held(pb.error is None, "B acquires the locks cleanly once A commits")

B.run("UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'bob'")
B.run("UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'alice'")
B.run("COMMIT")

rows = A.run("SELECT owner, balance_cents FROM accounts ORDER BY id")
total = A.scalar("SELECT sum(balance_cents) FROM accounts")
lab.held(
    total == 200000 and dict(rows)["alice"] == 100000,
    f"both transfers applied, balances back where they started, total {total}",
)


lab.takeaway(
    """
    "We were deadlocking on a two-row transfer — two requests taking the same
     pair of rows in opposite orders. Postgres detects the cycle after
     deadlock_timeout and aborts one side with 40P01. The fix wasn't a bigger
     lock, it was sorting: we lock the rows in primary-key order so the second
     transaction queues instead of forming a cycle. We kept the retry handler
     for 40P01 and 40001 regardless, because you never get to pick the victim."
    """
)
lab.finish()
