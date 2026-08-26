# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 02 — dirty / non-repeatable / phantom reads across isolation levels."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, reset  # noqa: E402

lab = Lab(
    "02 — READ ANOMALIES",
    "What READ COMMITTED and REPEATABLE READ actually buy you in Postgres.",
)
reset()

A = lab.session("A")  # the reader
B = lab.session("B")  # the writer


# ---------------------------------------------------------------------------
lab.section("Dirty read: unreachable in Postgres, at any setting")

A.run("BEGIN TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
level = A.scalar("SHOW transaction_isolation")
lab.held(
    level == "read uncommitted",
    f"the setting is accepted and reported back as '{level}'",
    "Note what this means: Postgres echoes the level you asked for, so you "
    "CANNOT tell from SHOW that the setting does nothing. The only way to know "
    "is behavioural — which is the next check.",
)

B.run("BEGIN")
B.run("UPDATE accounts SET balance_cents = 1 WHERE owner = 'alice'", note="uncommitted")

seen = A.scalar("SELECT balance_cents FROM accounts WHERE owner = 'alice'")
lab.held(
    seen == 100000,
    f"A reads {seen} — B's uncommitted write is invisible anyway",
    "READ UNCOMMITTED behaves exactly as READ COMMITTED. MVCC has no mechanism "
    "for exposing an uncommitted row version, so there is nothing to opt into. "
    "'Drop to read uncommitted for speed' is not an available trade here.",
)
A.run("COMMIT")
B.run("ROLLBACK")


# ---------------------------------------------------------------------------
lab.section("Non-repeatable read at READ COMMITTED (the default)")
lab.note("Every STATEMENT takes a fresh snapshot.")

A.run("BEGIN")
first = A.scalar("SELECT balance_cents FROM accounts WHERE owner = 'alice'")

B.run("BEGIN")
B.run("UPDATE accounts SET balance_cents = 50000 WHERE owner = 'alice'")
B.run("COMMIT")

second = A.scalar("SELECT balance_cents FROM accounts WHERE owner = 'alice'")
lab.broke(
    first != second,
    f"same query, same transaction, two answers: {first} then {second}",
    "A wrote nothing. Its own view of the row changed underneath it.",
)
A.run("COMMIT")


# ---------------------------------------------------------------------------
lab.section("The same read at REPEATABLE READ")
lab.note("One snapshot for the whole TRANSACTION.")

A.run("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
first = A.scalar("SELECT balance_cents FROM accounts WHERE owner = 'alice'")

B.run("BEGIN")
B.run("UPDATE accounts SET balance_cents = 25000 WHERE owner = 'alice'")
B.run("COMMIT", note="committed, and visible to everyone else")

second = A.scalar("SELECT balance_cents FROM accounts WHERE owner = 'alice'")
lab.held(
    first == second,
    f"A still reads {second} — its snapshot predates B's commit",
)
A.run("COMMIT")
after = A.scalar("SELECT balance_cents FROM accounts WHERE owner = 'alice'")
lab.held(after == 25000, f"outside the transaction A sees {after}")


# ---------------------------------------------------------------------------
lab.section("Phantom read at READ COMMITTED")
lab.note("Not a changed row — a changed result SET.")

A.run("BEGIN")
before = A.scalar("SELECT count(*) FROM uploads WHERE user_id = 5")
B.run("INSERT INTO uploads (user_id, storage_url) VALUES (5, 's3://phantom-1.jpg')")
after = A.scalar("SELECT count(*) FROM uploads WHERE user_id = 5")
lab.broke(
    after > before,
    f"the predicate matched {before} rows, then {after} — a row appeared",
)
A.run("COMMIT")


# ---------------------------------------------------------------------------
lab.section("Phantom read at REPEATABLE READ")
lab.note(
    """
    The SQL standard PERMITS phantoms at this level. Postgres prevents them
    anyway, because it implements true snapshot isolation rather than the
    minimum the standard asks for. Do not carry this assumption to MySQL.
    """
)

A.run("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
before = A.scalar("SELECT count(*) FROM uploads WHERE user_id = 5")
B.run("INSERT INTO uploads (user_id, storage_url) VALUES (5, 's3://phantom-2.jpg')")
after = A.scalar("SELECT count(*) FROM uploads WHERE user_id = 5")
lab.held(
    after == before,
    f"count stays at {after}; no phantom, despite the standard allowing one",
)
A.run("COMMIT")

lab.note(
    """
    What snapshot isolation still does NOT prevent: write skew — two
    transactions read an overlapping set and then write DISJOINT rows. No row
    is written twice, so there is no conflict to detect. That is scenario 06.
    """
)


lab.takeaway(
    """
    "Postgres defaults to READ COMMITTED, so each statement gets a fresh snapshot —
     you never see uncommitted data, but two reads in one transaction can disagree.
     Where a request needed a consistent view across several queries we opened it
     REPEATABLE READ, which in Postgres is real snapshot isolation, so it kills
     phantoms too. The operational catch is that a frozen snapshot blocks vacuum,
     so a long-running reporting transaction bloats the tables — we kept them short."
    """
)
lab.finish()
