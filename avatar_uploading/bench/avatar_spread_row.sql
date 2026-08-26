-- Control: the identical statement, spread across 1000 different rows.
-- Any difference between this and avatar_hot_row.sql is contention, not the
-- cost of the UPDATE itself.
\set uid random(1, 1000)
\set v random(1, 1000000)
BEGIN;
UPDATE users SET avatar_upload_id = :v, updated_at = clock_timestamp() WHERE id = :uid;
END;
