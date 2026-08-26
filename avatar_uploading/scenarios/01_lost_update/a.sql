-- ===========================================================================
-- Scenario 01 — LOST UPDATE — session A
-- Run each STEP, then switch to terminal B. Do not run ahead.
-- ===========================================================================

-- STEP A1 -------------------------------------------------------------------
-- Read the current value, the way an ORM would.
BEGIN;
SELECT value FROM counters WHERE name = 'page_views';
--   → 0.  Now go to terminal B and run STEP B1.


-- STEP A2 -------------------------------------------------------------------
-- Write back "what we read, plus one" as a literal. This is the bug.
UPDATE counters SET value = 1 WHERE name = 'page_views';
COMMIT;
--   Committed cleanly. Now go to terminal B and run STEP B2.


-- STEP A3 -------------------------------------------------------------------
-- Look at the damage. Two +1s happened. The counter says 1.
SELECT value FROM counters WHERE name = 'page_views';


-- ===========================================================================
-- FIX 1 — do the arithmetic in the statement
-- ===========================================================================

-- STEP A4 -------------------------------------------------------------------
BEGIN;
UPDATE counters SET value = value + 1 WHERE name = 'page_views';
--   Do NOT commit. Go to terminal B, run STEP B4, and watch it hang.


-- STEP A5 -------------------------------------------------------------------
-- While B is hanging, see who is blocking whom (run in a third terminal, or
-- just trust it): SELECT pg_blocking_pids(pid) ... — see observe/blocking.sql
COMMIT;
--   The instant this commits, B unblocks and re-reads the NEW value.


-- ===========================================================================
-- FIX 2 — SELECT ... FOR UPDATE, for when you must compute in app code
-- ===========================================================================

-- STEP A6 -------------------------------------------------------------------
BEGIN;
SELECT value FROM counters WHERE name = 'page_views' FOR UPDATE;
--   The row is now locked for writes. Go run STEP B6 and watch it block —
--   note that it blocks on the SELECT, before it ever reaches the UPDATE.


-- STEP A7 -------------------------------------------------------------------
UPDATE counters SET value = 4 WHERE name = 'page_views';   -- pretend app maths
COMMIT;


-- ===========================================================================
-- FIX 3 — optimistic locking with a version column
-- ===========================================================================

-- STEP A8 -------------------------------------------------------------------
SELECT value, version FROM counters WHERE name = 'page_views';
--   Remember both numbers. Go run STEP B8 (B reads the same pair).


-- STEP A9 -------------------------------------------------------------------
-- Substitute the version you just read. A wins the race.
UPDATE counters SET value = value + 1, version = version + 1
WHERE name = 'page_views' AND version = 1        -- <- your version here
RETURNING value, version;
--   1 row. Now run STEP B9 and watch B get ZERO rows back — that is the
--   signal to retry, and it is the whole point of the pattern.
