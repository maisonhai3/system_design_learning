-- Design B: append-only. No shared row exists, so there is nothing to contend on.
\set uid random(1, 1000)
\set v random(1, 1000000)
BEGIN;
INSERT INTO uploads (user_id, storage_url, is_avatar)
VALUES (:uid, 's3://bench/' || :v || '.jpg', false);
END;
