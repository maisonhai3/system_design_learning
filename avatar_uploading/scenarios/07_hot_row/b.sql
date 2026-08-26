-- ===========================================================================
-- Scenario 07 — THE HOT ROW — session B
-- Run each STEP only when terminal A tells you to — session A owns the
-- resets between parts, so running ahead gives confusing errors, not lessons.
-- ===========================================================================

-- STEP B1 -------------------------------------------------------------------
-- A holds the lock on user 1. This is another perfectly ordinary avatar
-- upload, from a different user's request, and it can do nothing but wait.
BEGIN;
UPDATE users SET avatar_upload_id = 1000, updated_at = clock_timestamp()
WHERE id = 1;
--   HANGS for exactly as long as A's transaction lasts.
--
--   This is the shape of the whole problem: throughput on this row is
--   1 / transaction_duration, and no amount of application servers changes
--   that number. Worse, while this request waits it is still holding a
--   database connection — which is how one contended row starves the pool for
--   endpoints that have nothing to do with avatars.
COMMIT;


-- STEP B4 -------------------------------------------------------------------
-- The identical statement, one row over. Instant.
BEGIN;
UPDATE users SET avatar_upload_id = 1000, updated_at = clock_timestamp()
WHERE id = 501;
COMMIT;
--   UPDATE is not slow. Contending on one row is slow. Those are different
--   claims, and only the second one is true.


-- STEP B6 -------------------------------------------------------------------
-- Append-only: A has an INSERT open and this does not care.
BEGIN;
INSERT INTO uploads (user_id, storage_url) VALUES (1, 's3://append-B.jpg');
COMMIT;
--   Two inserts create two different rows. There is no shared row, so there is
--   no lock to queue for — even though both writes are "for user 1".
