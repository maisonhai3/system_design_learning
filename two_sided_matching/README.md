# Two-Sided Matching — a job recommendation lab

A toy you can run and break, built around one question: **what is actually
different about recommending in a market with two sides?**

Real job postings, scraped from three public feeds. Synthetic candidates, mined
from the vocabulary of those postings. Postgres with pgvector. Both directions —
jobs for a candidate, candidates for a job — through one engine you can read in
an afternoon.

```bash
./lab.sh up        # postgres + pgvector on :5435
./lab.sh seed      # load and embed the checked-in snapshot — no network needed
./lab.sh serve     # http://localhost:8000
```

Then flip the switches and watch the ranking move.

---

## The thesis

The obvious design is: embed the resume, embed the job description, cosine
search, done. That design is genuinely useful and it is **not two-sided**, for a
reason that fits on one line:

```
cos(resume, jd) = resume · jd = jd · resume = cos(jd, resume)
```

A dot product does not care which argument you wrote first. So there is **one
score matrix**. "Jobs for a candidate" is a row of it; "candidates for a job" is
a column. Two views, one relation. No embedding model changes that — it is a
property of the operator.

Which means every genuinely two-sided thing lives *outside* the vector:

| | |
|---|---|
| **Eligibility** | A property of the pair. Both parties' non-negotiables, one predicate. Symmetric, and the same check in both directions. |
| **Desirability** | Two different functions over two different column sets. This is the asymmetry. |
| **Congestion** | A job can hire one person. Similarity has no idea. |
| **Retrieval** | Caps everything downstream, including the reranker you are proud of. |

The vector is the cheapest part of the system and the least of what makes it
work.

---

## The pipeline

```
      resume vector                                    2,000 jobs
            │                                               │
   1. RETRIEVE ─────────── cheap, wide, approximate ────────┤   ~300 rows
            │              vector ∪ skill-overlap (hybrid)
            │
   2. FILTER ───────────── eligibility: a GATE, not a score       ~50 rows
            │              (and 17% of postings state a salary,
            │               so decide what NULL means first)
            │
   3. SCORE ────────────── candidate_wants()   employer_wants()
            │              geometric mean within each side
            │              harmonic mean ACROSS the two sides
            │
   4. CONGEST ──────────── penalise jobs this serving round
            │              already crowded  (needs shared state)
            │
   5. RERANK ───────────── expensive, narrow, sees both docs      20 rows
                           together — cross-encoder or LLM
```

Every stage is a flag on one `Options` object, shared by the CLI, the web UI and
the scenario proofs. `--scoring cosine --filters off` gives you the tutorial
version to compare against.

---

## Where the data comes from

**Not LinkedIn.** Their User Agreement prohibits scraping, the pages are behind
an auth wall with active anti-bot measures, and the markup rotates — so a
LinkedIn scraper is a thing you repair weekly rather than build once, and none
of that difficulty teaches you anything about recommendation. This lab pulls
from three public, documented, no-auth feeds instead:

| source | what it gives you | rows |
|---|---|---|
| **Hacker News** "Ask HN: Who is hiring?" via the [Algolia API](https://hn.algolia.com/api) | long free-text posts — the best material here for embeddings | ~1,370 |
| **[Remote OK](https://remoteok.com)** ([`/api`](https://remoteok.com/api)) | structured, tagged, remote-only | ~100 |
| **[Arbeitnow](https://www.arbeitnow.com)** ([job board API](https://www.arbeitnow.com/api/job-board-api)) | structured, EU-heavy — makes the location filter mean something | ~600 |

*Job data from [Remote OK](https://remoteok.com), as their API terms request.*

The **candidates are synthetic**, and that is deliberate: resumes are personal
data, and the scrapeable ones are scraped without consent. Instead the personas
are mined from skill **co-occurrence in the scraped postings** — whatever the
real market is asking for this month is what these candidates know.

They share only *vocabulary* with the corpus, never phrasing. Building a resume
by copying sentences out of the job you want it to match is the leakage trap: the
resume contains the job, cosine hits ~1.0, retrieval looks perfect, and you have
measured string equality with extra steps. The scores here are lower than a
leaky generator would have shown you, and real.

```bash
./lab.sh scrape          # re-scrape (cached, polite, survives one feed dying)
./lab.sh synth           # regenerate candidates from the new corpus
./lab.sh load && ./lab.sh embed
```

---

## Read the data before you trust a filter

```
$ ./lab.sh doctor

job field coverage        (every hard filter is a bet on one of these)
  salary_max         314   16.8%  ####
  country           1424   76.4%  ###################
  min_years_exp      944   50.6%  ############
  sponsors_visa      126    6.8%  #
  of which min_years_exp inferred from a title word, not stated: 634
```

**17% of real job postings state a salary.** A strict salary filter is mostly a
filter on "did anyone bother to write it down" — it deletes 98% of the retrieved
pool and shows 14 of 60 candidates an empty page. This one table is the most
useful five seconds in the lab, and it is the thing a design doc never contains.

---

## The seven scenarios

Each one asserts two kinds of claim — **NAIVE BROKE** (the simple version really
does misbehave) and **FIX HELD** (the fix really does work) — and `./lab.sh
run-all` exits non-zero the moment either stops holding. A teaching lab that
quietly stops demonstrating its own bug is worse than none, because you would go
on believing it.

| # | scenario | the claim you'll be able to defend |
|---|---|---|
| 01 | **The symmetry trap** | `cos` is symmetric to the last bit, so vector search is one relation read from two ends. Widest disagreement found in the corpus: employer **1.00** vs candidate **0.14** on the same pair. |
| 02 | **Hard filters** | Strict filtering keeps **2%** of the pool. Soft-scoring eligibility leaks **44 ineligible jobs into 90 top-10 slots**. A filter is a gate, and a rejection needs a reason. |
| 03 | **Reciprocal ranking** | Harmonic, not arithmetic: **+8 points** of mutually-acceptable recommendations for one line of arithmetic. The same trap one level down cost this lab a bug. |
| 04 | **Congestion** | Gini **0.86**, coverage **32%** — every list is great, the market is a pyramid. The obvious fix moves nothing; the one that works costs you statelessness. |
| 05 | **Rerank** | Recall is capped by the *retriever*: the reranker's favourite job sits at cosine rank **1,032**. Depth cannot fix it; a second retrieval channel gets **+16 points**. |
| 06 | **Cold start** | Content-based works on day zero. And a plausible bonus claim — that congestion control also promotes new listings — is measured and **false**. |
| 07 | **ANN + filters** | The obvious pgvector query returns **4 rows for `LIMIT 20`**, silently — but only once the table is big enough that the planner uses the index. Plus the `AS MATERIALIZED` trap. |

```bash
./lab.sh list
./lab.sh read 01        # the one to start with
./lab.sh run 01
./lab.sh run-all
```

---

## Playing with it

```bash
./lab.sh candidates --focus data
./lab.sh suggest-jobs 4                          # the real thing
./lab.sh suggest-jobs 4 --scoring cosine --filters off    # the tutorial version
./lab.sh suggest-jobs 4 --retrieval hybrid --rerank
./lab.sh suggest-candidates 619 --congestion 2.0
./lab.sh serve                                   # same engine, clickable
```

Watch the **`wants→`** and **`←wanted`** columns. Under `--scoring cosine` they
are always equal. That equality is the whole bug.

Five candidates are hand-pinned as awkward cases the random generator would not
reliably produce:

| id | | why they exist |
|---|---|---|
| 1 | Priya Iyer | Textually perfect, 400k floor. Similarity and eligibility are different questions. |
| 2 | Chidi Okafor | Needs sponsorship; 7% of postings mention visas. |
| 3 | Alex Moreau | The generalist — never anyone's top match, always in everyone's pool. |
| 4, 5 | Yuki / Sora | Near-identical twins. Whatever tiebreak separates them is stable forever. |

---

## Schema

The version in your earlier notes was close. Three corrections, all in
`schema/01_schema.sql`:

- **Raw text is `TEXT`, structure is `JSONB`, and the columns you filter on are
  neither.** Promote `salary_max`, `country`, `remote`, `min_years_exp` into real
  columns — that is the only way they can be indexed, and JSONB filtering is what
  makes a query plan you cannot read.
- **`content_hash` covers the text *and the model*.** Hash only the text and
  switching embedders leaves every hash matching, nothing re-embeds, and half
  your corpus sits in a different vector space — where cosine still returns a
  number, and it sorts perfectly happily.
- **Both sides need a preference table, and they share no columns.** That
  asymmetry *is* the two-sidedness. `candidate_prefs` has a salary floor and a
  remote requirement; `jobs` has a years-of-experience minimum and visa
  sponsorship. Neither has a counterpart.

Every embedding is **384 dimensions** — the width of `all-MiniLM-L6-v2`. The
default embedder is a pure-numpy hashed BM25 that needs no download, but it is
pinned to MiniLM's width so graduating to a real model is a config change rather
than a migration. Pick your dimension for the model you intend to end up on.

---

## The embedder

Four backends, one interface, all 384-dim and L2-normalised:

| backend | notes |
|---|---|
| `bm25-hashed` | **default.** Offline, deterministic, ~1,000 docs/sec, numpy only. |
| `hashed-tfidf` | simpler, worse on long postings |
| `minilm` | real sentence embeddings; needs `sentence-transformers` + ~90MB |
| `openai` | `text-embedding-3-small` at 384 dims; needs `OPENAI_API_KEY` |

```bash
./lab.sh embed --backend minilm     # re-embeds everything, by design
```

The offline default is better than it sounds, and getting it there involved two
mistakes worth stealing:

**One hash bucket per term does not work.** 30,000 tokens into 384 buckets is
~78 terms per bucket, so two documents look similar whenever *any* of their
terms collide. It put "Commercial Building Estimator" at the top of a data
engineer's results. Spreading each term over 8 dimensions — a sparse random
projection — makes a term a *direction* rather than a bucket, and the error
falls like `1/sqrt(8)` instead of being all-or-nothing.

**Not all text in a document deserves equal weight.** Title and skills at 3x,
body at 1x (this is BM25F). Without it, 2,000 words of boilerplate about the
dental plan outvote the eight words naming the role. Top-10 results sharing at
least one skill with the candidate: **85.8%**.

---

## Layout

```
lab.sh                one entry point for everything
docker-compose.yml    pgvector/pgvector:pg16 on :5435
schema/01_schema.sql  the data model, commented as an argument not a reference

ingest/scrape.py      three public feeds -> fixtures/jobs.jsonl
ingest/synth.py       candidates mined from the scraped vocabulary
ingest/load.py        fixtures -> postgres, collapsing 200 monthly reposts
ingest/embed_all.py   idempotent embedding, invalidated by text AND model

lab/text.py           HTML, salary, location, seniority, skills out of prose
lab/embed.py          four backends behind one interface
lab/db.py             connections, and errors that tell you what to run
lab/harness.py        scenario output and the assertions that fail the build

match/engine.py       eligibility, both desirability functions, the combiners
match/query.py        retrieval SQL, including the hybrid channel
match/cli.py          suggest-jobs, suggest-candidates, doctor

web/                  FastAPI + one HTML file
scenarios/NN_name/    README.md (the argument) + run.py (the proof)
fixtures/             the checked-in snapshot, so it all runs offline
```

---

## Notes

**Port 5435.** 5432 is the machine's native cluster, 5433 belongs to
`avatar_uploading`, 5434 to `postgres_sandbox`.

**It runs offline.** `fixtures/` holds 2,067 scraped postings and 400 generated
candidates, so `./lab.sh seed` needs no network. `./lab.sh scrape` refreshes them
when you want this month's market instead.

**Verification status.** Everything here — schema, ingest, embeddings, all seven
scenario proofs, the CLI and the web UI — was built and run against
**PostgreSQL 16.13 with pgvector 0.6.0**. The Docker daemon was not available in
the environment where this was written, so `docker compose up` is the one path
that has not been executed end to end; the compose file is a standard
`pgvector/pgvector:pg16` service and the schema it mounts is the same file that
was verified. If it gives you trouble, point `LAB_DSN` at any Postgres with the
`vector` extension and everything else works unchanged:

```bash
LAB_DSN=postgresql://user:pass@host:5432/db ./lab.sh reset && ./lab.sh seed
```

**pgvector 0.6.0** is what was tested. `halfvec` and sparse vectors arrived in
0.7 and are not used here.

**Requirements:** Docker (or any pgvector Postgres), `psql`, and
[`uv`](https://docs.astral.sh/uv/) — the scripts declare their own dependencies
inline, so there is no virtualenv to manage.
