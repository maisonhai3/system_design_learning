-- Realtime Gateway Lab — source of truth.
--
-- Deliberately the SAME domain as this repo's redis_cache_lab: the same users,
-- the same datasets, the same grants. That is not laziness, it is the point.
--
-- redis_cache_lab asks "what does it cost to CACHE an authorization answer?"
-- This lab asks "what does it cost to PUSH one, and who is allowed to decide
-- it?" Reusing the domain lets you compare the two answers directly instead of
-- learning two toy worlds.

DROP TABLE IF EXISTS feed_deliveries;
DROP TABLE IF EXISTS dataset_grants;
DROP TABLE IF EXISTS datasets;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id           bigint PRIMARY KEY,
    email        text        NOT NULL UNIQUE,
    display_name text        NOT NULL,
    -- The RBAC half: coarse, in the token, checkable at the gateway.
    role         text        NOT NULL CHECK (role IN ('guest', 'member', 'admin')),
    org_id       bigint      NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE datasets (
    id             bigint PRIMARY KEY,
    org_id         bigint NOT NULL,
    name           text   NOT NULL,
    -- The ABAC half: visibility depends on an attribute of the DATA. The
    -- gateway cannot evaluate this, because the gateway has never seen this
    -- table. That single fact is what scenario 04 is about.
    classification text   NOT NULL CHECK (classification IN ('public', 'internal', 'restricted'))
);

CREATE TABLE dataset_grants (
    user_id    bigint NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    dataset_id bigint NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, dataset_id)
);

CREATE INDEX ON datasets (org_id);
CREATE INDEX ON dataset_grants (user_id);

-- An audit of what the fan-out actually delivered to whom.
--
-- It exists because scenario 02's hardest claim is a NEGATIVE one — "Bob's
-- stream never contained the restricted event" — and a negative claim about a
-- stream is only checkable if you also recorded the positive one. This is the
-- same reason you log authorization DENIALS and not just grants.
CREATE TABLE feed_deliveries (
    event_id   text        NOT NULL,
    user_id    bigint      NOT NULL,
    dataset_id bigint      NOT NULL,
    reason     text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, user_id)
);
