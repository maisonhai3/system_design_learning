-- ===========================================================================
-- Scenario 03 — THE AVATAR RACE — session A
-- Two devices upload an avatar at the same moment.
-- Run each STEP, then switch to terminal B. Do not run ahead.
-- ===========================================================================

-- STEP A0 -------------------------------------------------------------------
-- Where we start: user 1 has exactly one upload, and it is the avatar.
SELECT id, storage_url, is_avatar FROM uploads WHERE user_id = 1 ORDER BY id;


-- ===========================================================================
-- PART 1 — the two-statement write, with no constraint protecting it
-- ===========================================================================

-- STEP A1 -------------------------------------------------------------------
BEGIN;
UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar;
--   → UPDATE 1. The old avatar is demoted, but not committed yet.
--   Go to terminal B and run STEP B1. It will HANG on this row lock.


-- STEP A2 -------------------------------------------------------------------
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/from-phone.jpg', true);
COMMIT;
--   The instant this commits, B unblocks. Go watch what B's UPDATE reports.


-- STEP A3 -------------------------------------------------------------------
-- Run this only AFTER B has finished STEP B2.
SELECT id, storage_url, is_avatar FROM uploads WHERE user_id = 1 ORDER BY id;
--   Two rows with is_avatar = true. Nobody errored. Nobody rolled back.

SELECT count(*) AS live_avatars FROM uploads WHERE user_id = 1 AND is_avatar;

-- And this is what your API actually runs — it now returns two rows, so which
-- avatar the user sees depends on the plan, the page order, or which replica
-- answered. The avatar flickers.
SELECT storage_url FROM uploads WHERE user_id = 1 AND is_avatar = true;


-- ===========================================================================
-- PART 2 — the fix: make the invariant a constraint
-- ===========================================================================

-- STEP A4 -------------------------------------------------------------------
-- First clean up the mess Part 1 made, then declare the rule.
UPDATE uploads SET is_avatar = false WHERE user_id = 1;
UPDATE uploads SET is_avatar = true
WHERE id = (SELECT max(id) FROM uploads WHERE user_id = 1);

CREATE UNIQUE INDEX uploads_one_avatar_per_user
    ON uploads (user_id) WHERE is_avatar;
--   A partial index: it only indexes rows where is_avatar is true, so it costs
--   nothing on the other 99% of uploads, and it makes "one avatar per user" a
--   fact about the database rather than a hope about the application.


-- STEP A5 -------------------------------------------------------------------
-- Replay exactly the same race.
BEGIN;
UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar;
--   Go run STEP B5. It hangs again, same as before.


-- STEP A6 -------------------------------------------------------------------
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/from-phone-2.jpg', true);
COMMIT;
--   Now go watch B fail with 23505. That error is the fix working.


-- ===========================================================================
-- PART 3 — the trap: "just make it one atomic statement with a CTE"
-- ===========================================================================

-- STEP A7 -------------------------------------------------------------------
-- No concurrency here at all. Just you, one session, one statement.
WITH demoted AS (
    UPDATE uploads SET is_avatar = false WHERE user_id = 1 AND is_avatar
    RETURNING id
)
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/via-cte.jpg', true);
--   ERROR: 23505. Every time. Run it again — still 23505.
--
--   Every part of one statement, data-modifying CTEs included, shares a single
--   snapshot. The INSERT's uniqueness check still sees the index entry the CTE
--   is removing. "Atomic" does not mean "sequential".
--
--   Two ordinary statements inside one transaction work precisely BECAUSE the
--   second statement can see what the first one did.


-- ===========================================================================
-- PART 4 — ON CONFLICT: one round trip, and one nasty surprise
-- ===========================================================================

-- STEP A8 -------------------------------------------------------------------
SELECT count(*) AS rows_before FROM uploads WHERE user_id = 1;

INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/upserted.jpg', true)
ON CONFLICT (user_id) WHERE is_avatar DO UPDATE
    SET storage_url = EXCLUDED.storage_url,
        created_at  = clock_timestamp()
RETURNING id, storage_url, (xmax <> 0) AS was_an_update;
--   was_an_update = true. Note the id: it is the OLD row's id.

SELECT count(*) AS rows_after FROM uploads WHERE user_id = 1;
--   Identical to rows_before. No row was inserted. The previous avatar was
--   overwritten in place — the upload history you moved to this table to keep
--   has just been destroyed, silently, by the "recommended" fix.

-- Also worth knowing: you cannot omit the predicate. This is an error --
-- Postgres will not infer a partial index from a bare conflict target.
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://x', true)
ON CONFLICT (user_id) DO NOTHING;


-- ===========================================================================
-- PART 5 — append-only: no flag, no update, no contention
-- ===========================================================================

-- STEP A9 -------------------------------------------------------------------
DROP INDEX uploads_one_avatar_per_user;

BEGIN;
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (1, 's3://bucket/u1/append-A.jpg', false);
--   Do NOT commit. Go run STEP B9 — and notice it does NOT hang. Two inserts
--   touch two different rows, so there is nothing to contend over.


-- STEP A10 ------------------------------------------------------------------
COMMIT;

-- The read side. One row, deterministic, no flag needed:
SELECT id, storage_url, created_at
FROM uploads WHERE user_id = 1
ORDER BY created_at DESC LIMIT 1;

-- Full history still there:
SELECT count(*) AS total_uploads FROM uploads WHERE user_id = 1;

--   The cost you must name out loud: "newest wins" means you cannot revert to
--   an older avatar without a separate endpoint, and it trusts clock ordering
--   between writers. That is the trade, and it is usually worth it.
