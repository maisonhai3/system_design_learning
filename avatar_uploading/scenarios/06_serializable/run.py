# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 06 — write skew across all three isolation levels, then the retry loop."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, reset  # noqa: E402

FLOOR = 100000  # the rule: the accounts must hold at least this much between them

lab = Lab(
    "06 — WRITE SKEW / SERIALIZABLE",
    "The anomaly REPEATABLE READ does not catch, because the writes are disjoint.",
)
reset()

A = lab.session("A")
B = lab.session("B")


def attempt(level: str):
    """Run the classic write-skew interleaving at `level`.

    Ordering matters: both sessions write BEFORE either commits. That is what
    makes SSI defer its verdict to COMMIT time — see the last section.
    """
    A.conn.execute("UPDATE accounts SET balance_cents = 100000")

    A.run(f"BEGIN TRANSACTION ISOLATION LEVEL {level}")
    B.run(f"BEGIN TRANSACTION ISOLATION LEVEL {level}")

    # Both read the same total and both conclude a withdrawal is safe.
    A.scalar("SELECT sum(balance_cents) FROM accounts", note="A: plenty, proceed")
    B.scalar("SELECT sum(balance_cents) FROM accounts", note="B: plenty, proceed")

    plan = [
        (A, "UPDATE accounts SET balance_cents = balance_cents - %s WHERE owner = 'alice'", (FLOOR,), "UPDATE"),
        (B, "UPDATE accounts SET balance_cents = balance_cents - %s WHERE owner = 'bob'", (FLOOR,), "UPDATE"),
        (A, "COMMIT", None, "COMMIT"),
        (B, "COMMIT", None, "COMMIT"),
    ]

    failed: dict[str, tuple[str, str]] = {}
    for sess, sql, params, where in plan:
        if sess.name in failed:
            continue
        err = sess.try_run(sql, params)
        if err is not None:
            failed[sess.name] = (err.sqlstate, where)
            sess.run("ROLLBACK")

    outcomes = [f"{n} failed {code} at {where}" for n, (code, where) in failed.items()]
    return A.scalar("SELECT sum(balance_cents) FROM accounts"), outcomes or ["both committed"], failed


# ---------------------------------------------------------------------------
lab.section("READ COMMITTED")
lab.note("The rule: the two accounts must hold at least 100000 between them.")
total, outcomes, _ = attempt("READ COMMITTED")
lab.broke(
    total == 0,
    f"total is {total} — the invariant is violated ({', '.join(outcomes)})",
    "Each withdrawal was valid at the moment it was decided, and wrong by the "
    "time it committed.",
)


# ---------------------------------------------------------------------------
lab.section("REPEATABLE READ — the one people expect to save them")
total, outcomes, _ = attempt("REPEATABLE READ")
lab.broke(
    total == 0,
    f"total is STILL {total} ({', '.join(outcomes)})",
    "A wrote only alice; B wrote only bob. No row was written twice, so there "
    "is no first-updater-wins conflict for snapshot isolation to detect. "
    "Scenario 02 showed this level killing phantoms — this is what it lets "
    "through.",
)


# ---------------------------------------------------------------------------
lab.section("SERIALIZABLE")
total, outcomes, failed = attempt("SERIALIZABLE")
code, where = next(iter(failed.values()), (None, None))
lab.held(
    total == FLOOR and code == "40001",
    f"total is {total} — one transaction was rejected ({', '.join(outcomes)})",
    "SSI tracked the read/write dependencies, found a pattern no serial order "
    "could produce, and refused one of them.",
)
lab.held(
    where == "COMMIT",
    f"and it was rejected at {where} — every statement had already succeeded",
    "Code that assumes 'the statements worked, so the commit will work' is "
    "broken under SERIALIZABLE.",
)


# ---------------------------------------------------------------------------
lab.section("Where 40001 lands depends on the interleaving")
lab.note(
    """
    SSI aborts as soon as it can PROVE the dangerous structure. Above, both
    sessions wrote before either committed, so the proof only existed at commit
    time. Reorder it so A commits first and the proof exists earlier — B is
    rejected at its UPDATE instead. You cannot assume either, which is why the
    retry has to wrap the whole transaction rather than just the commit.
    """
)
A.conn.execute("UPDATE accounts SET balance_cents = 100000")

A.run("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
B.run("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
A.scalar("SELECT sum(balance_cents) FROM accounts")
B.scalar("SELECT sum(balance_cents) FROM accounts")

A.run("UPDATE accounts SET balance_cents = balance_cents - %s WHERE owner = 'alice'", (FLOOR,))
A.run("COMMIT", note="A commits FIRST this time")

err = B.try_run(
    "UPDATE accounts SET balance_cents = balance_cents - %s WHERE owner = 'bob'", (FLOOR,)
)
lab.held(
    err is not None and err.sqlstate == "40001",
    "with A already committed, B is rejected at the UPDATE, not at the COMMIT",
)
B.run("ROLLBACK")


# ---------------------------------------------------------------------------
lab.section("The price: you must write the retry loop")
lab.note(
    """
    40001 is not a bug report, it is the database saying "run this again".
    Which means the transaction must be safe to run twice — any side effect
    that escapes the database has to happen after the commit succeeds.
    """
)
A.conn.execute("UPDATE accounts SET balance_cents = 100000")

MAX_ATTEMPTS = 3
committed = False
for attempt_no in range(1, MAX_ATTEMPTS + 1):
    B.run("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
    total = B.scalar("SELECT sum(balance_cents) FROM accounts")

    if attempt_no == 1:
        # Force a conflict: A slips in and commits underneath B.
        A.run("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        A.scalar("SELECT sum(balance_cents) FROM accounts")
        A.run(
            "UPDATE accounts SET balance_cents = balance_cents - %s WHERE owner = 'alice'",
            (FLOOR,),
        )
        A.run("COMMIT")

    if total - FLOOR < FLOOR:
        B.run("ROLLBACK", note="the rule says decline — this is the CORRECT answer")
        committed = False
        break

    err = B.try_run(
        "UPDATE accounts SET balance_cents = balance_cents - %s WHERE owner = 'bob'",
        (FLOOR,),
    ) or B.try_run("COMMIT")

    if err is None:
        committed = True
        break
    B.run("ROLLBACK", note=f"attempt {attempt_no} lost the race, retrying")

final = A.scalar("SELECT sum(balance_cents) FROM accounts")
lab.held(
    not committed and final == FLOOR,
    f"the retry re-read fresh data and correctly declined; total {final}",
    "This is the whole point: the retry does not blindly repeat the write. It "
    "re-runs the decision against data that is now current.",
)


lab.takeaway(
    """
    "The bug was write skew — two requests read the same totals, each made a
     decision valid at read time, and each wrote a DIFFERENT row, so there was no
     write conflict for snapshot isolation to catch. REPEATABLE READ does not
     help; that's the part people get wrong. We put that one path under
     SERIALIZABLE with a retry-on-40001 wrapper, and made sure the transaction had
     no side effects outside the database so retrying was safe. Everything else
     stayed READ COMMITTED — SSI costs too much under contention to be a default."
    """
)
lab.finish()
