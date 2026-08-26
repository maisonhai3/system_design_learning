-- ===========================================================================
-- Scenario 07 — THE HOT ROW — session A
-- ===========================================================================

-- STEP A1 -------------------------------------------------------------------
-- The write the review told you to delete.
BEGIN;
UPDATE users SET avatar_upload_id = 999, updated_at = clock_timestamp()
WHERE id = 1;
--   The row lock is now held. It is held until COMMIT — not until the end of
--   this statement. Anything else in this transaction (an S3 confirmation, an
--   outbox insert, a thumbnail row) extends the hold.
--
--   Go run STEP B1 and watch it queue.


-- STEP A2 -------------------------------------------------------------------
-- Before committing, look at what B is doing. Run this in a THIRD terminal:
--     ./lab.sh watch
-- or paste observe/blocking.sql. You want to see:
--     wait_event_type = Lock,  wait_event = transactionid
-- That pair is the signature of row contention. Remember it — it is the answer
-- to "how did you know it was the database and not the app?"


-- STEP A3 -------------------------------------------------------------------
COMMIT;
--   B unblocks immediately. Its wait had nothing to do with how expensive the
--   UPDATE was, and everything to do with how long A's transaction lasted.


-- STEP A4 -------------------------------------------------------------------
-- The same statement against a DIFFERENT row. No contention at all.
BEGIN;
UPDATE users SET avatar_upload_id = 999, updated_at = clock_timestamp()
WHERE id = 500;
--   Go run STEP B4 — it targets row 501 and does not wait for a moment.
COMMIT;


-- STEP A5 -------------------------------------------------------------------
-- MVCC: every UPDATE writes a NEW row version. Nothing is updated in place.
VACUUM FULL users;
SELECT ctid, pg_relation_size('users') AS bytes FROM users WHERE id = 1;
SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'users';

DO $$ BEGIN
    FOR i IN 1..2000 LOOP
        UPDATE users SET updated_at = clock_timestamp() WHERE id = 1;
    END LOOP;
END $$;

SELECT pg_sleep(1);   -- stats are flushed asynchronously
SELECT ctid, pg_relation_size('users') AS bytes FROM users WHERE id = 1;
SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'users';
--   The ctid MOVED: the row is at a different physical location, because each
--   update wrote a new version. The table grew ~160 KB from 2000 updates to
--   one row. But note n_tup_hot_upd also went up by ~2000 — these were
--   heap-only tuple updates, the cheap path.


-- STEP A5b ------------------------------------------------------------------
-- Now index the column being updated, and run the identical 2000 updates.
CREATE INDEX users_avatar_idx ON users (avatar_upload_id);
VACUUM FULL users;
SELECT n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'users';

DO $$ BEGIN
    FOR i IN 1..2000 LOOP
        UPDATE users SET avatar_upload_id = i WHERE id = 1;
    END LOOP;
END $$;

SELECT pg_sleep(1);
SELECT n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'users';
--   ZERO of them were HOT this time. A HOT update requires that no index
--   covers any column you changed; once one does, every version needs its own
--   index entries and the index bloats alongside the table.
--
--   The lesson to carry: adding an index to a frequently-updated column costs
--   far more than the index's disk space.
DROP INDEX users_avatar_idx;


-- STEP A6 -------------------------------------------------------------------
-- The append-only alternative: no shared row, so nothing to contend on.
BEGIN;
INSERT INTO uploads (user_id, storage_url) VALUES (1, 's3://append-A.jpg');
--   Go run STEP B6. It does not wait either.
COMMIT;

-- Now measure it properly, with real numbers:
--     ./lab.sh run 07
--     ./lab.sh bench
