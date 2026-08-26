-- ===========================================================================
-- Scenario 04 — DEADLOCK — session A
-- ===========================================================================

SELECT id, owner, balance_cents FROM accounts ORDER BY id;
--   alice is id 1, bob is id 2. Remember that; it is the whole fix.


-- ===========================================================================
-- PART 1 — build the cycle
-- ===========================================================================

-- STEP A1 -------------------------------------------------------------------
-- A is sending money to bob, so it debits alice first.
BEGIN;
UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'alice';
--   A now holds the lock on alice. Go run STEP B1.


-- STEP A2 -------------------------------------------------------------------
-- Run this only after B1 has completed (B holds bob).
UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'bob';
--   HANGS — B holds bob. Now go run STEP B2 to close the cycle.
--
--   Within 500ms of B2, ONE of the two sessions dies with:
--     ERROR:  deadlock detected
--     SQLSTATE 40P01
--   It may well be this one. You do not get to choose.


-- STEP A3 -------------------------------------------------------------------
-- Run whichever applies: if A survived, COMMIT. If A was the victim, its
-- transaction is already aborted and only ROLLBACK is legal.
COMMIT;
-- ROLLBACK;


-- ===========================================================================
-- PART 2 — the fix: always take locks in the same order
-- ===========================================================================

-- STEP A4 -------------------------------------------------------------------
-- Both sessions now lock BOTH rows up front, sorted by id. Same order, always.
BEGIN;
SELECT id, owner FROM accounts WHERE owner IN ('alice','bob')
ORDER BY id
FOR UPDATE;
--   A holds both locks. Go run STEP B4 — it blocks, but it CANNOT deadlock,
--   because it is queueing for the same locks in the same order.


-- STEP A5 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'alice';
UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'bob';
COMMIT;
--   B now unblocks and completes its own transfer. No cycle, no victim.


-- STEP A6 -------------------------------------------------------------------
SELECT owner, balance_cents FROM accounts ORDER BY id;
SELECT sum(balance_cents) AS total FROM accounts;
--   Total is unchanged: both transfers applied, nothing lost.
