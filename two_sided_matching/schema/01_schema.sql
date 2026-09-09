-- Two-sided matching: the schema.
--
-- Read this file top to bottom and you have the whole data model. There are
-- three ideas in it, and only the third one is really about recommendation:
--
--   1. Documents.   Free text + whatever structure we could parse out of it.
--   2. Vectors.     One per document, in the same row. No separate vector DB.
--   3. PREFERENCES. Both sides have them, and THEY ARE NOT THE SAME SHAPE.
--
-- (3) is the entire reason this lab exists. A candidate cares about salary
-- floor, location, and remote. An employer cares about years of experience,
-- seniority, and whether they can sponsor a visa. Those are different columns
-- on different tables, so "does A want B" and "does B want A" are different
-- questions with different answers. Cosine similarity cannot tell them apart
-- (see scenarios/01), which is why the vector is the smallest part of this file.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- for the lexical half of hybrid search

-- Every embedding in this lab is 384-dimensional. That is not arbitrary: it is
-- the width of sentence-transformers/all-MiniLM-L6-v2. The default embedder
-- here is a pure-numpy hashed TF-IDF that needs no model download, but it emits
-- 384 dims too -- so you can graduate to a real model without a migration.
-- Pick your dimension for the model you intend to end up on, not the one you
-- started with; changing it later rewrites every row and every index.


-- ---------------------------------------------------------------------------
-- Side A: candidates
-- ---------------------------------------------------------------------------

CREATE TABLE candidates (
    id              bigserial PRIMARY KEY,
    full_name       text        NOT NULL,
    email           text        NOT NULL UNIQUE,
    headline        text,
    years_exp       numeric(4,1),
    seniority       text,          -- junior | mid | senior | staff | principal
    current_title   text,
    location        text,
    country         text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The candidate's side of the two-sided preference. Nullable on purpose:
-- "no preference" and "wants >= 150k" are different states, and collapsing
-- NULL to 0 silently turns every unfilled profile into a match for everything.
CREATE TABLE candidate_prefs (
    candidate_id      bigint PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    min_salary        integer,       -- floor. Job's MAX must clear this.
    remote_required   boolean NOT NULL DEFAULT false,
    willing_locations text[]  NOT NULL DEFAULT '{}',
    needs_visa        boolean NOT NULL DEFAULT false,
    min_company_size  integer,
    open_to_contract  boolean NOT NULL DEFAULT true
);

CREATE TABLE resumes (
    id              bigserial PRIMARY KEY,
    candidate_id    bigint NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,

    raw_text        text  NOT NULL,   -- TEXT, not JSONB. Prose is not structured.
    parsed          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- skills[], sections, titles

    -- The vector lives in the same row as the text it was made from. At this
    -- scale (and at a lot more than this scale) a separate vector database is
    -- an extra thing to keep in sync for no benefit you can measure.
    embedding       vector(384),

    -- Re-embedding hygiene. content_hash is the answer to "did the text
    -- actually change, or did someone just touch the row?" -- re-embedding is
    -- the expensive step, so you want to skip it when the bytes are identical.
    -- embedding_model is the answer to "can I compare these two vectors?"
    -- (you cannot, across models -- see scenarios/07).
    content_hash    text,
    embedding_model text,
    embedded_at     timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (candidate_id)             -- one live resume per candidate in this toy
);


-- ---------------------------------------------------------------------------
-- Side B: employers and jobs
-- ---------------------------------------------------------------------------

CREATE TABLE employers (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL,
    website       text,
    company_size  integer,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name)
);

CREATE TABLE jobs (
    id            bigserial PRIMARY KEY,
    employer_id   bigint NOT NULL REFERENCES employers(id) ON DELETE CASCADE,

    title         text NOT NULL,
    status        text NOT NULL DEFAULT 'open',   -- open | filled | closed
    location      text,
    country       text,
    remote        boolean NOT NULL DEFAULT false,
    employment    text,                           -- fulltime | contract | intern

    -- Salary is a RANGE and both ends are nullable, because in the real feeds
    -- this lab scrapes, most postings simply do not state one. `lab.sh doctor`
    -- prints the coverage. Roughly 5% of RemoteOK rows and 60% of Hacker News
    -- rows have a parseable salary. Any hard filter you write on this column is
    -- really a filter on "did we manage to parse it", which is why scenarios/02
    -- makes you choose what NULL means before it will rank anything.
    salary_min    integer,
    salary_max    integer,
    currency      text DEFAULT 'USD',

    -- The employer's side of the two-sided preference. Note that not one of
    -- these columns has a counterpart in candidate_prefs, and vice versa.
    -- That asymmetry is the thing being taught.
    min_years_exp   numeric(4,1),
    seniority       text,
    sponsors_visa   boolean NOT NULL DEFAULT false,
    headcount       integer NOT NULL DEFAULT 1,   -- how many people they can hire

    source        text,        -- hn | remoteok | arbeitnow | synthetic
    source_id     text,        -- natural key at the source, for idempotent re-ingest
    source_url    text,
    posted_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (source, source_id)
);

CREATE TABLE job_docs (
    job_id          bigint PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    raw_text        text  NOT NULL,
    parsed          jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding       vector(384),
    content_hash    text,
    embedding_model text,
    embedded_at     timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- What actually happened
-- ---------------------------------------------------------------------------
--
-- Without this table you have a similarity engine, not a recommender. It is
-- what cold start is the absence of (scenarios/06) and what congestion shows
-- up in (scenarios/04): if every candidate is shown the same twelve jobs, this
-- table is where you can see it, and it is the only place you can see it.

CREATE TABLE interactions (
    id            bigserial PRIMARY KEY,
    candidate_id  bigint NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id        bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind          text   NOT NULL,   -- impression | click | save | apply | invite | hire
    actor         text   NOT NULL,   -- 'candidate' or 'employer' -- who did it
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX interactions_job_kind_idx  ON interactions (job_id, kind);
CREATE INDEX interactions_cand_kind_idx ON interactions (candidate_id, kind);


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Hard-filter columns get real indexes. They are separate columns rather than
-- JSONB keys precisely so this line can exist.
CREATE INDEX jobs_open_idx      ON jobs (status) WHERE status = 'open';
CREATE INDEX jobs_remote_idx    ON jobs (remote) WHERE status = 'open';
CREATE INDEX jobs_salary_idx    ON jobs (salary_max) WHERE status = 'open';
CREATE INDEX jobs_country_idx   ON jobs (country);
CREATE INDEX jobs_title_trgm    ON jobs USING gin (title gin_trgm_ops);

-- The lexical half of hybrid retrieval (match/query.py:_retrieve_jobs_hybrid)
-- asks "which postings mention any of these skills". Without this index that
-- is a sequential scan plus a jsonb_array_elements_text per row -- fine at
-- 2,000 rows, quadratic-feeling at 200,000.
CREATE INDEX job_docs_skills_gin ON job_docs USING gin ((parsed -> 'skills'));
CREATE INDEX resumes_skills_gin  ON resumes  USING gin ((parsed -> 'skills'));

-- The ANN indexes. Deliberately NOT created here -- scenarios/07 has you
-- measure exact search first, then add these and measure what you gave up.
-- Left as a comment so you type them yourself and feel the build cost:
--
--   SET maintenance_work_mem = '512MB';
--   CREATE INDEX job_docs_hnsw ON job_docs
--       USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
--   CREATE INDEX resumes_hnsw ON resumes
--       USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
--
-- vector_cosine_ops, not vector_l2_ops: every embedder in this lab L2-normalises
-- its output, and the query operator (<=>) must match the operator class or the
-- index is silently ignored. "Silently" is the part that costs you an afternoon.
