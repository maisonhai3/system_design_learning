-- ===========================================================================
-- Scenario 05 — SKIP LOCKED — session B (worker B)
-- Run each STEP only when terminal A tells you to — session A owns the
-- resets between parts, so running ahead gives confusing errors, not lessons.
-- ===========================================================================

-- ===========================================================================
-- PART 1 — no locking: everyone takes the same job
-- ===========================================================================

-- STEP B1 -------------------------------------------------------------------
BEGIN;
SELECT id, payload FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1;
--   → job 1. The SAME job A just claimed. A plain SELECT takes no lock and
--   makes no promise that anyone else is not reading the same row.


-- STEP B2 -------------------------------------------------------------------
UPDATE jobs SET state = 'running', locked_by = 'B' WHERE id = 1;
COMMIT;
--   No error. B's write simply overwrites A's. Both workers now process job 1.
--   If the job sends an email or charges a card, it happens twice.


-- ===========================================================================
-- PART 2 — FOR UPDATE: correct, and single-threaded
-- ===========================================================================

-- STEP B3 -------------------------------------------------------------------
BEGIN;
SELECT id, payload FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1
FOR UPDATE;
--   HANGS. It is waiting for job 1 specifically — the row A holds — while
--   jobs 2 through 50 sit unclaimed the whole time.
--
--   When A commits, this re-runs its scan and returns job 2. The result is
--   correct. The throughput is one worker's worth, no matter how many workers
--   you deploy.


-- STEP B4 -------------------------------------------------------------------
UPDATE jobs SET state = 'running', locked_by = 'B' WHERE id = 2;
COMMIT;


-- ===========================================================================
-- PART 3 — SKIP LOCKED: correct AND parallel
-- ===========================================================================

-- STEP B5 -------------------------------------------------------------------
BEGIN;
SELECT id, payload FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1
FOR UPDATE SKIP LOCKED;
--   → job 2, immediately. No wait at all. The scan stepped over row 1 because
--   someone else holds it, and returned the next free row.


-- STEP B6 -------------------------------------------------------------------
UPDATE jobs SET state = 'running', locked_by = 'B' WHERE id = 2;
COMMIT;
--   Go run STEP A7.
