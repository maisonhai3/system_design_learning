-- ===========================================================================
-- Scenario 04 — DEADLOCK — session B
-- Run each STEP only when terminal A tells you to — session A owns the
-- resets between parts, so running ahead gives confusing errors, not lessons.
-- ===========================================================================

-- ===========================================================================
-- PART 1 — build the cycle
-- ===========================================================================

-- STEP B1 -------------------------------------------------------------------
-- B is sending money to alice, so it debits bob first. Opposite order to A.
BEGIN;
UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'bob';
--   B now holds the lock on bob. Go run STEP A2 (it will hang).


-- STEP B2 -------------------------------------------------------------------
-- This closes the cycle: B wants alice, which A holds; A wants bob, which B
-- holds.
UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'alice';
--   Hangs for up to 500ms (deadlock_timeout), then Postgres runs its detector
--   and kills ONE of the two sessions with 40P01.
--
--   Run `./lab.sh logs` in a third terminal: the server prints the full cycle,
--   both queries, and which process it chose to abort.


-- STEP B3 -------------------------------------------------------------------
COMMIT;
-- ROLLBACK;      -- if B was the victim, this is the only legal option


-- ===========================================================================
-- PART 2 — the fix: always take locks in the same order
-- ===========================================================================

-- STEP B4 -------------------------------------------------------------------
-- Note this is IDENTICAL to A's STEP A4 — same rows, same ORDER BY. That
-- sameness is the entire fix.
BEGIN;
SELECT id, owner FROM accounts WHERE owner IN ('alice','bob')
ORDER BY id
FOR UPDATE;
--   Blocks, waiting for A. It will NOT deadlock: it is queueing behind A for
--   the same locks in the same order, which is an ordinary wait, not a cycle.
--   Wait for A to run STEP A5.


-- STEP B5 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = balance_cents - 100 WHERE owner = 'bob';
UPDATE accounts SET balance_cents = balance_cents + 100 WHERE owner = 'alice';
COMMIT;
