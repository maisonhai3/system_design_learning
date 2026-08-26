-- ===========================================================================
-- Scenario 01 — LOST UPDATE — session B
-- Run each STEP only when terminal A tells you to.
-- ===========================================================================

-- STEP B1 -------------------------------------------------------------------
-- B reads the same value A did. Neither transaction has written yet.
BEGIN;
SELECT value FROM counters WHERE name = 'page_views';
--   → 0, the same 0 A saw. Go back to A and run STEP A2.


-- STEP B2 -------------------------------------------------------------------
-- B writes back "0 + 1" too. No error. No conflict. No block.
UPDATE counters SET value = 1 WHERE name = 'page_views';
COMMIT;
--   Go back to A and run STEP A3 to see the counter say 1 after two +1s.


-- ===========================================================================
-- FIX 1 — do the arithmetic in the statement
-- ===========================================================================

-- STEP B4 -------------------------------------------------------------------
-- A is holding an uncommitted UPDATE on this row. This will HANG.
BEGIN;
UPDATE counters SET value = value + 1 WHERE name = 'page_views';
--   Blocked. Go run STEP A5 (COMMIT) and watch this return instantly.
--   Crucially, when it unblocks it re-reads the row and adds 1 to A's NEW
--   value. Nothing is lost.
COMMIT;

SELECT value FROM counters WHERE name = 'page_views';   -- → 3, not 2


-- ===========================================================================
-- FIX 2 — SELECT ... FOR UPDATE
-- ===========================================================================

-- STEP B6 -------------------------------------------------------------------
-- A holds FOR UPDATE on this row. This SELECT blocks — a plain SELECT would
-- NOT have. FOR UPDATE is what makes readers queue.
BEGIN;
SELECT value FROM counters WHERE name = 'page_views' FOR UPDATE;
--   Blocked until A runs STEP A7. When it returns you see A's committed
--   value, so your app-side arithmetic is now based on fresh data.
UPDATE counters SET value = 5 WHERE name = 'page_views';
COMMIT;


-- ===========================================================================
-- FIX 3 — optimistic locking
-- ===========================================================================

-- STEP B8 -------------------------------------------------------------------
SELECT value, version FROM counters WHERE name = 'page_views';
--   Same version A read. Go run STEP A9.


-- STEP B9 -------------------------------------------------------------------
-- B still thinks the version is what it read at B8. It is not, any more.
UPDATE counters SET value = value + 1, version = version + 1
WHERE name = 'page_views' AND version = 1        -- <- the STALE version
RETURNING value, version;
--   UPDATE 0. Zero rows. No error was raised: your application code has to
--   check the row count and retry. Forgetting that check is how optimistic
--   locking silently becomes no locking at all.


-- STEP B10 ------------------------------------------------------------------
-- The retry: re-read, then write against the version you just saw.
SELECT value, version FROM counters WHERE name = 'page_views';
UPDATE counters SET value = value + 1, version = version + 1
WHERE name = 'page_views' AND version = 2        -- <- the FRESH version
RETURNING value, version;
