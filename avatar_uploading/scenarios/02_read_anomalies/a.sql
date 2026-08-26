-- ===========================================================================
-- Scenario 02 — READ ANOMALIES — session A (the reader)
-- ===========================================================================

-- STEP A1 — dirty read: prove it cannot happen ------------------------------
BEGIN TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SHOW transaction_isolation;
--   → "read uncommitted".  Postgres echoes back exactly what you asked for,
--   so SHOW will NOT tell you the setting is a no-op. It is: READ UNCOMMITTED
--   behaves as READ COMMITTED. MVCC has no mechanism for showing you an
--   uncommitted row version, so there is nothing to opt into. The only way to
--   see this is behaviourally — STEP A2 proves it.
--   Now go run STEP B1 (B makes an uncommitted change).


-- STEP A2 -------------------------------------------------------------------
SELECT balance_cents FROM accounts WHERE owner = 'alice';
--   → 100000, the committed value. B's uncommitted write is invisible.
COMMIT;


-- STEP A3 — non-repeatable read at READ COMMITTED ----------------------------
BEGIN;   -- READ COMMITTED, the default
SELECT balance_cents FROM accounts WHERE owner = 'alice';
--   Note the number. Go run STEP B3 (B updates AND commits).


-- STEP A4 -------------------------------------------------------------------
SELECT balance_cents FROM accounts WHERE owner = 'alice';
--   Different number, same transaction, no writes of your own. Every
--   STATEMENT takes a fresh snapshot at READ COMMITTED.
COMMIT;


-- STEP A5 — the same read at REPEATABLE READ --------------------------------
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT balance_cents FROM accounts WHERE owner = 'alice';
--   Note the number. Go run STEP B5 (B updates AND commits again).


-- STEP A6 -------------------------------------------------------------------
SELECT balance_cents FROM accounts WHERE owner = 'alice';
--   IDENTICAL to A5, even though B committed in between. The snapshot was
--   taken once, for the whole transaction.
COMMIT;

SELECT balance_cents FROM accounts WHERE owner = 'alice';
--   And now, outside the transaction, you see B's change.


-- STEP A7 — phantoms -------------------------------------------------------
BEGIN;   -- READ COMMITTED
SELECT count(*) FROM uploads WHERE user_id = 5;
--   Go run STEP B7 (B inserts a row matching that predicate and commits).


-- STEP A8 -------------------------------------------------------------------
SELECT count(*) FROM uploads WHERE user_id = 5;
--   The count grew. A row appeared inside your transaction: a phantom.
COMMIT;


-- STEP A9 — phantoms at REPEATABLE READ -------------------------------------
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT count(*) FROM uploads WHERE user_id = 5;
--   Go run STEP B9.


-- STEP A10 ------------------------------------------------------------------
SELECT count(*) FROM uploads WHERE user_id = 5;
--   Unchanged. The SQL standard ALLOWS phantoms at this level; Postgres
--   prevents them anyway, because it implements true snapshot isolation.
--   Do not carry that assumption to MySQL or Oracle.
COMMIT;
