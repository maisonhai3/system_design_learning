-- Avatar Uploading Lab — seed data.
--
-- 1000 users, because bench/avatar_spread_row.sql needs enough distinct rows
-- that random(1, 1000) genuinely spreads writes across pages.

INSERT INTO users (email, display_name)
SELECT format('user%s@example.com', i), format('User %s', i)
FROM generate_series(1, 1000) AS i;

-- Everybody already has an avatar. Scenario 03's race needs an existing
-- is_avatar = true row to fight over; starting from zero hides the bug.
INSERT INTO uploads (user_id, storage_url, is_avatar)
SELECT id, format('s3://bucket/u%s/original.jpg', id), true
FROM users;

UPDATE users u
SET avatar_upload_id = up.id
FROM uploads up
WHERE up.user_id = u.id;

INSERT INTO accounts (owner, balance_cents) VALUES
    ('alice', 100000),
    ('bob',   100000);

INSERT INTO counters (name, value) VALUES ('page_views', 0);

-- 200 shards so scenario 07 can contrast one hot row against spread writes.
INSERT INTO counters (name, value)
SELECT format('shard_%s', i), 0 FROM generate_series(0, 199) AS i;

INSERT INTO jobs (payload)
SELECT format('resize-avatar-%s', i) FROM generate_series(1, 50) AS i;
