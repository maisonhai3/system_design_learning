# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 07 — measure what the hot row actually costs."""

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import psycopg  # noqa: E402

from lab.harness import DSN, Lab, reset  # noqa: E402

WRITERS = 20
HOLD = 0.05  # how long each writer keeps its transaction open

lab = Lab(
    "07 — THE HOT ROW",
    f"{WRITERS} concurrent writers, {HOLD * 1000:.0f}ms each. Same statement, different targets.",
)
reset()

A = lab.session("A")
B = lab.session("B")


# ---------------------------------------------------------------------------
lab.section("First, see the lock with your own eyes")

A.run("BEGIN")
A.run(
    "UPDATE users SET avatar_upload_id = 999, updated_at = clock_timestamp() WHERE id = 1"
)
B.run("BEGIN")
pending = B.spawn(
    "UPDATE users SET avatar_upload_id = 1000, updated_at = clock_timestamp() WHERE id = 1"
)
lab.broke(
    pending.still_blocked_after(1.0),
    "a second avatar upload for the same user blocks for A's whole transaction",
    "The lock is held until COMMIT, not until the end of the statement. Any "
    "S3 call or outbox insert left inside the transaction extends the hold.",
)

waits = A.conn.execute(
    """SELECT wait_event_type || ':' || wait_event
       FROM pg_stat_activity
       WHERE application_name = 'session_B' AND wait_event IS NOT NULL"""
).fetchall()
lab.broke(
    ("Lock:transactionid",) in waits,
    f"pg_stat_activity shows B waiting on {waits[0][0] if waits else 'nothing'}",
    "Lock:transactionid is the signature of row contention. This is how you "
    "answer 'how did you know it was the database and not the app?'",
)

A.run("COMMIT")
pending.wait()
B.run("COMMIT")

# The identical statement against a different row does not wait at all.
A.run("BEGIN")
A.run("UPDATE users SET updated_at = clock_timestamp() WHERE id = 500")
B.run("BEGIN")
pending = B.spawn("UPDATE users SET updated_at = clock_timestamp() WHERE id = 501")
lab.held(
    not pending.still_blocked_after(0.7),
    "the same UPDATE against a different row does not wait at all",
    "UPDATE is not slow. Contending on one row is slow. Only the second claim "
    "is true, and only when the row is genuinely hot.",
)
pending.wait()
A.run("COMMIT")
B.run("COMMIT")


# ---------------------------------------------------------------------------
lab.section(f"Now measure it: {WRITERS} writers x {HOLD * 1000:.0f}ms")

HOT = "UPDATE users SET avatar_upload_id = %s, updated_at = clock_timestamp() WHERE id = 1"
SPREAD = "UPDATE users SET avatar_upload_id = %s, updated_at = clock_timestamp() WHERE id = %s"
APPEND = "INSERT INTO uploads (user_id, storage_url) VALUES (%s, %s)"


def writer(kind: str, i: int, errors: list):
    try:
        with psycopg.connect(DSN, autocommit=True, application_name=f"w{i}") as c:
            c.execute("BEGIN")
            if kind == "hot":
                c.execute(HOT, (i,))
            elif kind == "spread":
                c.execute(SPREAD, (i, i + 1))
            else:
                c.execute(APPEND, (i + 1, f"s3://bench/{i}.jpg"))
            time.sleep(HOLD)  # stand-in for the rest of the transaction
            c.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def measure(kind: str) -> float:
    errors: list = []
    threads = [
        threading.Thread(target=writer, args=(kind, i, errors)) for i in range(WRITERS)
    ]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    if errors:
        raise errors[0]
    return elapsed


results = {kind: measure(kind) for kind in ("hot", "spread", "append")}

floor = HOLD
print()
print(f"  {'design':<34}{'elapsed':>9}{'vs ideal':>11}")
print(f"  {'-' * 54}")
labels = {
    "hot":    "UPDATE users ... WHERE id = 1",
    "spread": "UPDATE users ... WHERE id = <own>",
    "append": "INSERT INTO uploads ...",
}
for kind, secs in results.items():
    print(f"  {labels[kind]:<34}{secs:8.2f}s{secs / floor:10.1f}x")
print()

ratio = results["hot"] / results["spread"]
lab.broke(
    results["hot"] > HOLD * (WRITERS * 0.5),
    f"the hot row took {results['hot']:.2f}s — the writers ran one after another",
    f"{WRITERS} writers x {HOLD}s, fully serialized. Throughput on a row is "
    f"1 / transaction_duration, and adding app servers does not change it.",
)
lab.held(
    ratio > 5,
    f"spreading the same UPDATE across rows was {ratio:.0f}x faster "
    f"({results['spread']:.2f}s)",
)
lab.held(
    results["append"] < HOLD * 4,
    f"append-only INSERTs finished in {results['append']:.2f}s — no shared row at all",
)


# ---------------------------------------------------------------------------
lab.section("MVCC: every UPDATE writes a new row version")
lab.note(
    """
    Postgres never updates in place. Each UPDATE writes a NEW version of the
    row and leaves the old one for vacuum. 2000 updates to one logical row
    means 2000 physical versions.
    """
)


def table_stats() -> dict:
    """Read the stats collector, waiting for the backend to flush."""
    A.conn.execute("SELECT pg_stat_clear_snapshot()")
    upd, hot = A.conn.execute(
        """SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables
           WHERE relname = 'users'"""
    ).fetchone()
    size = A.conn.execute("SELECT pg_relation_size('users')").fetchone()[0]
    ctid = A.conn.execute("SELECT ctid::text FROM users WHERE id = 1").fetchone()[0]
    return {"upd": upd, "hot": hot, "size": size, "ctid": ctid}


def hammer(column_sql: str, label: str) -> dict:
    """2000 updates to user 1, then wait for the stats to catch up."""
    A.conn.execute("VACUUM FULL users")
    before = table_stats()
    A.run(
        f"""DO $$ BEGIN
              FOR i IN 1..2000 LOOP
                UPDATE users SET {column_sql} WHERE id = 1;
              END LOOP;
            END $$""",
        note=label,
    )
    # Stats are flushed asynchronously; poll rather than guess at a sleep.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        after = table_stats()
        if after["upd"] - before["upd"] >= 1900:
            break
        time.sleep(0.2)
    return {
        "hot": after["hot"] - before["hot"],
        "grew": after["size"] - before["size"],
        "ctid_before": before["ctid"],
        "ctid_after": after["ctid"],
    }


unindexed = hammer("updated_at = clock_timestamp()", "column is NOT indexed")
lab.held(
    unindexed["ctid_before"] != unindexed["ctid_after"],
    f"the row physically moved: ctid {unindexed['ctid_before']} -> {unindexed['ctid_after']}",
    "Proof there is no in-place update. Each write placed a new version "
    "somewhere else in the heap.",
)
lab.broke(
    unindexed["grew"] > 100_000,
    f"the table grew {unindexed['grew'] // 1024} KB from 2000 updates to ONE row",
    "That is the autovacuum bill a genuinely hot row generates continuously.",
)
lab.held(
    unindexed["hot"] > 1900,
    f"{unindexed['hot']} of 2000 were HOT updates (heap-only tuples)",
    "Because no index covers `updated_at`, Postgres can chain versions inside "
    "the page and skip index maintenance entirely. This is the good case.",
)

lab.note(
    """
    Now index the column being updated, and run exactly the same 2000 updates.
    """
)
A.run("CREATE INDEX users_avatar_idx ON users (avatar_upload_id)")
indexed = hammer("avatar_upload_id = i", "column IS indexed")
A.run("DROP INDEX users_avatar_idx")

lab.broke(
    indexed["hot"] == 0,
    f"with the column indexed, {indexed['hot']} of 2000 updates were HOT",
    "Every single version now needs its own index entries, so the index bloats "
    "alongside the table. Updating an INDEXED column on a hot row costs "
    "dramatically more than updating an unindexed one — worth knowing before "
    "you add an index to a column your write path touches.",
)


lab.note(
    """
    The honest tradeoff: append-only wins this benchmark, but users.avatar_upload_id
    gives you the avatar in the same row as the user — one lookup, no join.
    You are trading read simplicity for write concurrency. Say that out loud.

    Real tps numbers at -c 1/10/50/100:  ./lab.sh bench
    What happens when the waiters eat the pool:  ./lab.sh exhaust
    """
)


lab.takeaway(
    f"""
    "The bottleneck wasn't CPU or disk, it was one row. The current avatar was a
     column on `users`, so every upload updated that user's row, and the row lock
     is held for the whole transaction — throughput per row is one over transaction
     duration. I measured it: {WRITERS} concurrent writers against one row took
     {ratio:.0f}x longer than the same {WRITERS} against different rows. The second-order
     effect is what actually took the API down — each waiter holds a connection
     while it blocks, so one contended row exhausted the pool and starved endpoints
     that had nothing to do with avatars."
    """
)
lab.finish()
