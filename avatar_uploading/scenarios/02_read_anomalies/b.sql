-- ===========================================================================
-- Scenario 02 — READ ANOMALIES — session B (the writer)
-- Run each STEP only when terminal A tells you to — session A owns the
-- resets between parts, so running ahead gives confusing errors, not lessons.
-- ===========================================================================

-- STEP B1 — an uncommitted write, left hanging ------------------------------
BEGIN;
UPDATE accounts SET balance_cents = 1 WHERE owner = 'alice';
--   Do NOT commit. Go run STEP A2 — A cannot see this, at any isolation level.


-- STEP B2 -------------------------------------------------------------------
ROLLBACK;


-- STEP B3 — a committed write, mid-transaction for A ------------------------
BEGIN;
UPDATE accounts SET balance_cents = 50000 WHERE owner = 'alice';
COMMIT;
--   Go run STEP A4. A sees the new value inside its own open transaction.


-- STEP B5 -------------------------------------------------------------------
BEGIN;
UPDATE accounts SET balance_cents = 25000 WHERE owner = 'alice';
COMMIT;
--   Go run STEP A6. This time A does NOT see it.


-- STEP B7 — a phantom row ---------------------------------------------------
INSERT INTO uploads (user_id, storage_url) VALUES (5, 's3://phantom-1.jpg');
--   Go run STEP A8. A's count grew mid-transaction.


-- STEP B9 -------------------------------------------------------------------
INSERT INTO uploads (user_id, storage_url) VALUES (5, 's3://phantom-2.jpg');
--   Go run STEP A10. This time A's count does not move.
