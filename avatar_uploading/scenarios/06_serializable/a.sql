-- ===========================================================================
-- Scenario 06 — WRITE SKEW — session A
-- The rule: accounts must hold at least 100000 between them.
-- ===========================================================================

SELECT owner, balance_cents FROM accounts ORDER BY id;
SELECT sum(balance_cents) AS total FROM accounts;    -- 200000


-- ===========================================================================
-- PART 1 — READ COMMITTED: the invariant breaks
-- ===========================================================================

-- STEP A1 -------------------------------------------------------------------
BEGIN;
SELECT sum(balance_cents) AS total FROM accounts;
--   200000. Taking 100000 leaves 100000, which satisfies the rule. Proceed.
--   Go run STEP B1 — B reads the same total and reaches the same conclusion.


-- STEP A2 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = balance_cents - 100000 WHERE owner = 'alice';
COMMIT;
--   Go run STEP B2.


-- STEP A3 -------------------------------------------------------------------
SELECT sum(balance_cents) AS total FROM accounts;
--   ZERO. Two withdrawals, each valid when it was decided, both committed.
--   Note that A wrote only alice and B wrote only bob: no row was written
--   twice, so there was no conflict for the database to notice.


-- ===========================================================================
-- PART 2 — REPEATABLE READ: still breaks (this is the surprising one)
-- ===========================================================================

-- STEP A4 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = 100000;     -- reset

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT sum(balance_cents) AS total FROM accounts;
--   Go run STEP B4.


-- STEP A5 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = balance_cents - 100000 WHERE owner = 'alice';
COMMIT;
--   Go run STEP B5, then STEP A6.


-- STEP A6 -------------------------------------------------------------------
SELECT sum(balance_cents) AS total FROM accounts;
--   ZERO again. Snapshot isolation gave both transactions a stable view and
--   still let them both commit, because they wrote DIFFERENT rows. This is
--   write skew, and it is precisely the hole REPEATABLE READ does not close.


-- ===========================================================================
-- PART 3 — SERIALIZABLE: the invariant holds
-- ===========================================================================

-- STEP A7 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = 100000;     -- reset

BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
SELECT sum(balance_cents) AS total FROM accounts;
--   Go run STEP B7. B reads the same total AND does its UPDATE, without
--   committing. The order matters here — see the note at STEP B8.


-- STEP A8 -------------------------------------------------------------------
UPDATE accounts SET balance_cents = balance_cents - 100000 WHERE owner = 'alice';
COMMIT;
--   A commits fine. Now go run STEP B8 and watch B fail AT COMMIT.


-- STEP A9 -------------------------------------------------------------------
SELECT sum(balance_cents) AS total FROM accounts;
--   100000. The invariant survived, because one transaction was rejected.
