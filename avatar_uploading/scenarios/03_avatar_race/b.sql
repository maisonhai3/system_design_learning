-- ===========================================================================
-- Scenario 03 — THE AVATAR RACE — session B
-- The second device. Run each STEP only when terminal A tells you to.
-- ===========================================================================

-- ===========================================================================
-- PART 1 — the two-statement write, with no constraint protecting it
-- ===========================================================================

-- STEP B1 -------------------------------------------------------------------
-- A holds an uncommitted demote on the old avatar row. This HANGS.
BEGIN;
UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar;
--
--   >>> While it hangs, this is the whole bug in one line. Read on. <<<
--
--   When A commits, this returns UPDATE 0 — ZERO rows.
--
--   At READ COMMITTED, a blocked UPDATE re-evaluates its WHERE clause against
--   the newly committed row. The row now has is_avatar = false, so it no longer
--   matches, so B demotes nothing. B's "clear the old avatar" step silently
--   did not happen, and B was not told.


-- STEP B2 -------------------------------------------------------------------
-- B now inserts its own avatar, believing it cleared the previous one.
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/from-laptop.jpg', true);
COMMIT;
--   Committed. No error. Go back to A and run STEP A3 to see two live avatars.


-- ===========================================================================
-- PART 2 — the fix: make the invariant a constraint
-- ===========================================================================

-- STEP B5 -------------------------------------------------------------------
-- Same race, but now uploads_one_avatar_per_user exists.
BEGIN;
UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar;
--   Hangs on A's lock, then returns UPDATE 0 exactly as before. The demote
--   still silently no-ops — the index does not prevent that.

INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/from-laptop-2.jpg', true);
--   ERROR:  duplicate key value violates unique constraint
--           "uploads_one_avatar_per_user"
--   SQLSTATE 23505.
--
--   THIS IS THE FIX WORKING. The index cannot stop B from racing; what it does
--   is convert an outcome your application thinks is impossible into an error
--   your application is forced to handle. Silent corruption became a loud,
--   retryable failure.

ROLLBACK;

--   In production the handler is: catch 23505, re-read, retry once. The retry
--   succeeds because by then A's write is committed and visible.


-- ===========================================================================
-- PART 5 — append-only: no flag, no update, no contention
-- ===========================================================================

-- STEP B9 -------------------------------------------------------------------
-- A has an uncommitted INSERT open. Watch this NOT hang.
BEGIN;
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/append-B.jpg', false);
COMMIT;
--   Instant. Two INSERTs create two different rows; there is no shared row to
--   lock, so there is nothing to wait for. This is why the append-only design
--   wins the benchmark in scenario 07 — not because inserts are fast, but
--   because they do not queue.
