# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]"]
# ///
"""Scenario 03 — the avatar race. The one this lab exists for."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from lab.harness import Lab, reset  # noqa: E402

USER = 1

lab = Lab(
    "03 — THE AVATAR RACE",
    "Two devices upload an avatar at once. Nobody errors. The user gets two.",
)
reset()

A = lab.session("A")
B = lab.session("B")


def uploads_state(label: str = ""):
    rows = A.conn.execute(
        "SELECT count(*) FROM uploads WHERE user_id = %s", (USER,)
    ).fetchone()[0]
    avatars = A.conn.execute(
        "SELECT count(*) FROM uploads WHERE user_id = %s AND is_avatar", (USER,)
    ).fetchone()[0]
    return rows, avatars


# ---------------------------------------------------------------------------
lab.section("The anomaly: UPDATE-then-INSERT with no constraint")
lab.note(
    """
    Exactly the write the review recommended:
        UPDATE uploads SET is_avatar = false WHERE user_id = X AND is_avatar;
        INSERT INTO uploads (...) VALUES (..., true);
    """
)

A.run("BEGIN")
A.run(
    "UPDATE uploads SET is_avatar = false WHERE user_id = %s AND is_avatar",
    (USER,),
    note="A demotes the old avatar",
)

B.run("BEGIN")
b_demote = B.spawn(
    "UPDATE uploads SET is_avatar = false WHERE user_id = %s AND is_avatar", (USER,)
)
lab.held(
    b_demote.still_blocked_after(1.0),
    "B's demote blocks on A's row lock — the locking works fine",
)

A.run(
    "INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, true)",
    (USER, "s3://bucket/u1/from-phone.jpg"),
)
A.run("COMMIT")

b_demote.wait()
lab.broke(
    b_demote.rows is None,
    "B's demote unblocks and matches ZERO rows — it silently no-ops",
    "At READ COMMITTED a blocked UPDATE re-evaluates its WHERE against the "
    "newly committed row. is_avatar is already false, so it no longer matches. "
    "B is never told its demote did nothing.",
)

B.run(
    "INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, true)",
    (USER, "s3://bucket/u1/from-laptop.jpg"),
)
B.run("COMMIT", note="also succeeds")

rows, avatars = uploads_state()
lab.broke(
    avatars == 2,
    f"user {USER} now has {avatars} rows with is_avatar = true",
    "Both transactions committed. No error was raised anywhere.",
)

serving = A.run(
    "SELECT storage_url FROM uploads WHERE user_id = %s AND is_avatar = true", (USER,)
)
lab.broke(
    len(serving) == 2,
    "the production read query returns 2 rows for a single-valued field",
    "Which avatar the user sees now depends on plan order, page order, or "
    "which replica answered. It flickers.",
)


# ---------------------------------------------------------------------------
lab.section("Fix: make the invariant a constraint, not a convention")
lab.note(
    """
    A partial unique index costs nothing on non-avatar uploads, because it only
    indexes rows where the predicate is true.
    """
)
reset()

A.run(
    "CREATE UNIQUE INDEX uploads_one_avatar_per_user ON uploads (user_id) WHERE is_avatar"
)

A.run("BEGIN")
A.run("UPDATE uploads SET is_avatar = false WHERE user_id = %s AND is_avatar", (USER,))
B.run("BEGIN")
b_demote = B.spawn(
    "UPDATE uploads SET is_avatar = false WHERE user_id = %s AND is_avatar", (USER,)
)
b_demote.still_blocked_after(0.7)

A.run(
    "INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, true)",
    (USER, "s3://bucket/u1/from-phone-2.jpg"),
)
A.run("COMMIT")
b_demote.wait()

err = B.try_run(
    "INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, true)",
    (USER, "s3://bucket/u1/from-laptop-2.jpg"),
)
lab.held(
    err is not None and err.sqlstate == "23505",
    f"B's INSERT fails with {err.sqlstate if err else 'no error'} instead of corrupting",
    "The index cannot stop the race. What it does is convert an impossible "
    "state into a loud, retryable error — which is the whole job.",
)
B.run("ROLLBACK")

rows, avatars = uploads_state()
lab.held(avatars == 1, f"exactly {avatars} live avatar survives the race")
lab.held(rows == 2, f"upload history intact: {rows} rows kept for the user")


# ---------------------------------------------------------------------------
lab.section("The trap: 'just wrap it in a CTE to make it atomic'")
lab.note(
    """
    No concurrency in this section at all. One session, one statement.
    """
)

err = A.try_run(
    """
    WITH demoted AS (
        UPDATE uploads SET is_avatar = false WHERE user_id = %s AND is_avatar
        RETURNING id
    )
    INSERT INTO uploads (user_id, storage_url, is_avatar)
    VALUES (%s, %s, true)
    """,
    (USER, USER, "s3://bucket/u1/via-cte.jpg"),
)
lab.broke(
    err is not None and err.sqlstate == "23505",
    "the 'atomic' CTE form fails with 23505 — single-threaded, every time",
    "All parts of one statement share one snapshot, data-modifying CTEs "
    "included. The INSERT's uniqueness check still sees the index entry the "
    "CTE is removing. Atomic does not mean sequential — which is exactly why "
    "two plain statements in one transaction DO work.",
)


# ---------------------------------------------------------------------------
lab.section("ON CONFLICT: one round trip, and one nasty surprise")
reset()
A.run(
    "CREATE UNIQUE INDEX uploads_one_avatar_per_user ON uploads (user_id) WHERE is_avatar"
)

before, _ = uploads_state()
result = A.run(
    """
    INSERT INTO uploads (user_id, storage_url, is_avatar)
    VALUES (%s, %s, true)
    ON CONFLICT (user_id) WHERE is_avatar DO UPDATE
        SET storage_url = EXCLUDED.storage_url,
            created_at  = clock_timestamp()
    RETURNING id, (xmax <> 0) AS was_an_update
    """,
    (USER, "s3://bucket/u1/upserted.jpg"),
)
after, avatars = uploads_state()

lab.held(avatars == 1, "the invariant holds, in a single round trip, with no race")
lab.broke(
    after == before and result[0][1] is True,
    f"NO row was inserted: {before} rows before, {after} after — it updated in place",
    "The previous avatar was overwritten. If you moved avatars into `uploads` "
    "to keep upload history, this 'fix' silently deletes the thing you were "
    "trying to keep.",
)

err = A.try_run(
    """INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, true)
       ON CONFLICT (user_id) DO NOTHING""",
    (USER, "s3://x"),
)
lab.broke(
    err is not None and err.sqlstate == "42P10",
    "omitting the predicate is rejected — a partial index is never inferred",
    "You must restate the index's WHERE clause in the conflict target.",
)


# ---------------------------------------------------------------------------
lab.section("Append-only: no flag, no update, no contention at all")
reset()

A.run("BEGIN")
A.run(
    "INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, false)",
    (USER, "s3://bucket/u1/append-A.jpg"),
)
B.run("BEGIN")
b_insert = B.spawn(
    "INSERT INTO uploads (user_id, storage_url, is_avatar) VALUES (%s, %s, false)",
    (USER, "s3://bucket/u1/append-B.jpg"),
)
lab.held(
    not b_insert.still_blocked_after(0.7),
    "B's INSERT does not block on A's open transaction",
    "Two inserts create two different rows. There is no shared row to lock, "
    "so there is nothing to queue behind.",
)
b_insert.wait()
A.run("COMMIT")
B.run("COMMIT")

current = A.run(
    """SELECT storage_url FROM uploads WHERE user_id = %s
       ORDER BY created_at DESC LIMIT 1""",
    (USER,),
)
rows, _ = uploads_state()
lab.held(len(current) == 1, "the read returns exactly one row, deterministically")
lab.held(rows == 3, f"full history preserved: {rows} uploads kept")
lab.note(
    """
    Name the cost out loud: newest-wins means reverting to an older avatar
    needs its own endpoint, and it trusts clock ordering between writers.
    """
)


lab.takeaway(
    """
    "Moving the pointer off `users` removed a hot row but lost the invariant —
     `is_avatar` was a convention, not a constraint, so two concurrent uploads
     each demoted the old row, each inserted a new one, and the user ended up
     with two live avatars and no error. The fix was the partial unique index:
     it turns silent corruption into a 23505 we retry. We rejected the
     ON CONFLICT upsert because it updates in place and would have destroyed
     the upload history. We shipped append-only with newest-row-wins, which has
     no write contention at all, and added a separate endpoint for reverting."
    """
)
lab.finish()
