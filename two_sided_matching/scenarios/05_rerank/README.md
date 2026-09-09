# 05 — Retrieve, then rerank

**The claim:** the two-stage cascade is right, and the number everyone tunes
(pool depth) is not the one that limits it.

## Stage 2 earns its place

Reranking replaces about **3 of every 10** top-10 results. Below roughly 1,
delete the stage and keep the latency.

## You cannot rerank everything

The lab's stand-in reranker is free and instant. A cross-encoder or LLM is
about 40ms per pair:

```
  pool depth   with a real reranker
  50                   2.0 s
  200                  8.0 s
  1865                74.6 s          <- and this catalogue is tiny
```

Hence the cascade: cheap and wide, then expensive and narrow.

## Depth does not fix recall

Treat "rerank every eligible job" as ground truth and measure what a shallow
pool recovers:

```
  pool retrieved   -> eligible   recall@10
  200                       52      53.0%
  400                      103      63.0%
  800                      200      75.0%     <- 43% of the catalogue reranked
```

Two separate things are wrong here, and the middle column is the first.

### The pool is not the number you think it is

A pool of 200 retrieved rows leaves about **52 eligible** ones. Retrieval ranks
by vector distance and knows nothing about salary or location, so the filter
eats the pool *after* the depth was chosen:

```
  rerankable items = pool depth x filter pass rate
```

Every "we retrieve the top 200" in a design doc is really "we retrieve however
many survive", and nobody writes down the second number.

### And the real ceiling

Where do the ideal results sit in the *retriever's own* ordering?

```
  candidate   best   median   deepest
  2              1       69       986
  4              6      103      1032
  20            15      259       786
```

A job the reranker would have picked first sits at cosine rank **1,032**. No
pool shallower than that will ever hand it over. A cascade can only reorder
what stage 1 surfaces — **recall is capped by the retriever, not by the
reranker or the budget.**

## The fix: give stage 1 the signal stage 2 is using

The reranker's main signal is exact skill overlap. The embedding blurs it —
"airflow" becomes a projection into 384 dimensions shared with thousands of
other terms. So add a second retrieval channel that does not blur: top half of
the pool by vector distance, top half by skill-set intersection, unioned.

```
  pool   vector only   hybrid    delta
  200         53.0%     53.0%     +0.0
  400         63.0%     79.0%    +16.0
```

+16 points for one extra SQL branch and no change to the reranker. Quadrupling
the pool cost 4x the rerank bill for less.

**This is what hybrid search is actually for.** Not "vectors plus keywords
because both are good" — the two channels fail on *different* documents, so the
union has a higher ceiling than either. If your reranker uses a signal your
retriever cannot see, that signal needs its own retrieval channel.

```bash
./lab.sh run 05
./lab.sh suggest-jobs 4 --retrieval hybrid --rerank
```
