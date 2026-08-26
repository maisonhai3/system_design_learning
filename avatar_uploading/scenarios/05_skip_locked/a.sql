-- ===========================================================================
-- Scenario 05 — SKIP LOCKED — session A (worker A)
-- ===========================================================================

SELECT count(*) FILTER (WHERE state = 'pending') AS pending FROM jobs;   -- 50


-- ===========================================================================
-- PART 1 — no locking: everyone takes the same job
-- ===========================================================================

-- STEP A1 -------------------------------------------------------------------
BEGIN;
SELECT id, payload FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1;
--   → job 1. Go run STEP B1 and watch B claim the SAME job.


-- STEP A2 -------------------------------------------------------------------
UPDATE jobs SET state = 'running', locked_by = 'A' WHERE id = 1;
COMMIT;
--   Go run STEP B2. B overwrites locked_by with its own name. Both workers
--   believe they own job 1, and 49 other jobs went untouched.


-- ===========================================================================
-- PART 2 — FOR UPDATE: correct, and single-threaded
-- ===========================================================================

-- STEP A3 -------------------------------------------------------------------
UPDATE jobs SET state = 'pending', locked_by = NULL;   -- reset

BEGIN;
SELECT id, payload FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1
FOR UPDATE;
--   → job 1, and A now holds the row lock. Go run STEP B3.
--   Note how long B sits there. Jobs 2..50 are free the entire time.


-- STEP A4 -------------------------------------------------------------------
UPDATE jobs SET state = 'running', locked_by = 'A' WHERE id = 1;
COMMIT;
--   B unblocks now, re-runs its scan, and takes job 2. Correct — but B spent
--   the whole of A's transaction doing nothing. Add ten workers and you get
--   ten workers queued on row 1. That is a lock convoy.


-- ===========================================================================
-- PART 3 — SKIP LOCKED: correct AND parallel
-- ===========================================================================

-- STEP A5 -------------------------------------------------------------------
UPDATE jobs SET state = 'pending', locked_by = NULL;   -- reset

BEGIN;
SELECT id, payload FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1
FOR UPDATE SKIP LOCKED;
--   → job 1, locked by A. Go run STEP B5 — it returns job 2 INSTANTLY.


-- STEP A6 -------------------------------------------------------------------
UPDATE jobs SET state = 'running', locked_by = 'A' WHERE id = 1;
COMMIT;


-- STEP A7 -------------------------------------------------------------------
SELECT id, state, locked_by FROM jobs WHERE locked_by IS NOT NULL ORDER BY id;
--   Two workers, two different jobs, neither ever waited.
