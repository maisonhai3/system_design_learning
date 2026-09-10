-- Redis Cache Lab — source of truth.
--
-- Small on purpose. The lab is about what happens BETWEEN Postgres and Redis,
-- so the schema only needs to be rich enough to have (a) a row worth caching,
-- (b) a permission worth caching, and (c) a per-user data slice worth caching.
-- Those three are the three things people cache, and they fail differently.

DROP TABLE IF EXISTS cache_invalidations;
DROP TABLE IF EXISTS dataset_grants;
DROP TABLE IF EXISTS datasets;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id           bigint PRIMARY KEY,
    email        text        NOT NULL UNIQUE,
    display_name text        NOT NULL,
    -- The RBAC half: a coarse role, the thing a router-level guard checks.
    role         text        NOT NULL CHECK (role IN ('guest', 'member', 'admin')),
    org_id       bigint      NOT NULL,
    -- The row's write counter. Every UPDATE bumps it (see the trigger below).
    --
    -- This exists because of scenario 03: a cache entry keyed only by id cannot
    -- tell "the value I read 3ms ago" from "the value someone committed 1ms
    -- ago". A monotonic version turns that unanswerable question into a string
    -- comparison. It is the cheapest form of optimistic concurrency there is.
    version      bigint      NOT NULL DEFAULT 1,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE datasets (
    id             bigint PRIMARY KEY,
    org_id         bigint NOT NULL,
    name           text   NOT NULL,
    -- The ABAC half: visibility depends on an attribute of the DATA, not only
    -- on who is asking. "Restricted" rows need an explicit grant.
    classification text   NOT NULL CHECK (classification IN ('public', 'internal', 'restricted'))
);

CREATE TABLE dataset_grants (
    user_id    bigint NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    dataset_id bigint NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, dataset_id)
);

CREATE INDEX ON datasets (org_id);
CREATE INDEX ON dataset_grants (user_id);

-- Why a trigger instead of `version = version + 1` in the repository?
--
-- Because the moment the version bump lives in application code, correctness
-- depends on every future code path remembering to do it: the admin backfill,
-- the data-fix script someone runs in psql at 2am, the second service that got
-- write access "just this once". A cache-consistency scheme that a raw UPDATE
-- can silently defeat is not a scheme, it is a convention.
--
-- Push the invariant down to the layer that cannot be bypassed. Same reasoning
-- as a CHECK constraint versus a Pydantic validator: keep both, but only one
-- of them is actually load-bearing.
CREATE OR REPLACE FUNCTION bump_version() RETURNS trigger AS $$
BEGIN
    NEW.version    := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_bump_version
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION bump_version();

-- The durable version of "remember to invalidate", used in scenario 02.
--
-- FastAPI's BackgroundTasks lives in the process's memory. A SIGKILL, an OOM,
-- a pod eviction mid-deploy, and the invalidation is simply gone — while the
-- data change it belonged to is safely committed. That asymmetry is the whole
-- problem: the write is durable and its consequence is not.
--
-- Writing the intent into the SAME transaction as the data change fixes the
-- asymmetry by construction. Either both are committed or neither is; there is
-- no interleaving in which the row changed and the invalidation was forgotten.
-- A separate relay drains this table and issues the DEL, retrying until Redis
-- accepts it.
--
-- This is the transactional outbox pattern. It is the same one this repo's
-- student_course_enrollment lab uses for message publishing, for the same
-- reason: you cannot make two systems atomic, so you make one of them the
-- record of intent for the other.
CREATE TABLE cache_invalidations (
    id         bigserial PRIMARY KEY,
    cache_key  text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    drained_at timestamptz
);

-- Partial index: the relay only ever asks for undrained rows, and this keeps
-- that query O(backlog) instead of O(history).
CREATE INDEX cache_invalidations_pending
    ON cache_invalidations (id) WHERE drained_at IS NULL;
