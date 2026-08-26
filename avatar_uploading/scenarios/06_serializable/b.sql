-- ===========================================================================
-- Scenario 06 — WRITE SKEW — session B
-- Run each STEP only when terminal A tells you to — session A owns the
-- resets between parts, so running ahead gives confusing errors, not lessons.
-- ===========================================================================

-- ===========================================================================
-- PART 1 — READ COMMITTED
-- ===========================================================================

-- STEP B1 -------------------------------------------------------------------
BEGIN;
SELECT sum(balance_cents) AS total FROM accounts;
--   200000, same as A saw. B concludes its withdrawal is safe too.
--   Go run STEP A2.


-- STEP B2 -------------------------------------------------------------------
-- Note: B writes BOB. A wrote ALICE. Different rows — nothing to conflict on.
UPDATE accounts SET balance_cents = balance_cents - 100000 WHERE owner = 'bob';
COMMIT;
--   Committed. No error. Go run STEP A3 to see the total hit zero.


-- ===========================================================================
-- PART 2 — REPEATABLE READ
-- ===========================================================================

-- STEP B4 -------------------------------------------------------------------
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT sum(balance_cents) AS total FROM accounts;
--   Go run STEP A5.


-- STEP B5 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = balance_cents - 100000 WHERE owner = 'bob';
COMMIT;
--   Still no error. If B had tried to update ALICE here it would have failed
--   with 40001 — snapshot isolation does catch two writers on one row. It is
--   the disjoint writes that slip through.


-- ===========================================================================
-- PART 3 — SERIALIZABLE
-- ===========================================================================

-- STEP B7 -------------------------------------------------------------------
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
SELECT sum(balance_cents) AS total FROM accounts;

UPDATE accounts SET balance_cents = balance_cents - 100000 WHERE owner = 'bob';
--   This SUCCEEDS. Watch carefully — the failure is not here.
--   Do NOT commit. Go run STEP A8.


-- STEP B8 -------------------------------------------------------------------
COMMIT;
--   ERROR:  could not serialize access due to read/write dependencies
--           among transactions
--   SQLSTATE 40001
--
--   The transaction ran to completion and was rejected on the way OUT. Any
--   code that assumes "the statements worked, so the commit will work" is
--   broken under SERIALIZABLE.
--
--   WHERE the error lands depends on the interleaving. SSI aborts as soon as
--   it can PROVE the cycle. Here both sessions wrote before either committed,
--   so the proof only existed at commit time. Had A committed BEFORE your
--   UPDATE at B7, the proof would have existed earlier and the UPDATE itself
--   would have raised 40001. You cannot rely on either — which is why the
--   retry must wrap the whole transaction, not just the COMMIT.


-- STEP B9 -------------------------------------------------------------------
-- The retry. This is not optional — 40001 means "run me again", and it is the
-- price of the guarantee.
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
SELECT sum(balance_cents) AS total FROM accounts;
--   100000 now. The rule says stop, so the retry correctly declines to
--   withdraw. That is the invariant doing its job.
ROLLBACK;
