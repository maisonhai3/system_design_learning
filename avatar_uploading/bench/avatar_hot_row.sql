-- Design A: the current avatar lives on the users row.
-- Every client in this run updates THE SAME row, which is what makes it hot.
\set v random(1, 1000000)
BEGIN;
UPDATE users SET avatar_upload_id = :v, updated_at = clock_timestamp() WHERE id = 1;
END;
