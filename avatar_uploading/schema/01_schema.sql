-- Avatar Uploading Lab — base schema.
SET client_min_messages = warning;
--
-- Idempotent on purpose: this file runs at container init AND every time you
-- run `./lab.sh reset`, so it must be safe to apply over an existing database.
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO PUBLIC;


-- ---------------------------------------------------------------------------
-- The design under review.
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id               bigserial PRIMARY KEY,
    email            text NOT NULL UNIQUE,
    display_name     text NOT NULL,

    -- This column is the thing the review told you to delete. Keep it: you
    -- cannot argue that removing it was worth doing until you have measured
    -- what it costs. Scenario 07 and bench/ measure exactly that.
    avatar_upload_id bigint,

    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE uploads (
    id          bigserial PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    storage_url text NOT NULL,
    is_avatar   boolean NOT NULL DEFAULT false,

    -- clock_timestamp(), NOT now(). now() returns the *transaction* start time,
    -- so every row inserted by one transaction would share a timestamp and the
    -- "ORDER BY created_at DESC LIMIT 1" design in scenario 03 would look
    -- artificially well-behaved. clock_timestamp() reads the real wall clock.
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX uploads_user_id_idx ON uploads (user_id);

-- NOTE: the partial unique index that makes "one avatar per user" an actual
-- invariant is deliberately NOT created here. Scenario 03 has you add it by
-- hand, after you have watched the schema fail without it.


-- ---------------------------------------------------------------------------
-- Supporting tables for the other scenarios.
-- ---------------------------------------------------------------------------

-- Scenario 01 (lost update) and 07 (hot row).
CREATE TABLE counters (
    name    text PRIMARY KEY,
    value   bigint NOT NULL DEFAULT 0,
    version bigint NOT NULL DEFAULT 0  -- for the optimistic-locking fix
);

-- Scenario 04 (deadlock). The CHECK is what makes an overdraft a real error
-- rather than a silently negative balance.
CREATE TABLE accounts (
    id            bigserial PRIMARY KEY,
    owner         text NOT NULL UNIQUE,
    balance_cents bigint NOT NULL CHECK (balance_cents >= 0)
);

-- Scenario 05 (FOR UPDATE SKIP LOCKED).
CREATE TYPE job_state AS ENUM ('pending', 'running', 'done');

CREATE TABLE jobs (
    id         bigserial PRIMARY KEY,
    payload    text NOT NULL,
    state      job_state NOT NULL DEFAULT 'pending',
    locked_by  text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX jobs_pending_idx ON jobs (id) WHERE state = 'pending';
