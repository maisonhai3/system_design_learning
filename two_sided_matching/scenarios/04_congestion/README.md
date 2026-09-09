# 04 — Congestion

**The claim:** a recommender can give every single user an excellent list and
still break the market, and no per-user metric will tell you.

## The failure

Give 250 candidates their top 10. Every list is eligible, well-matched, and
looks fine. Now aggregate over *jobs* instead of users:

```
  Gini of recommendation counts        0.860
  catalogue coverage                   32.2%
  share of all slots, top 20 jobs      25.3%
```

Twenty postings absorb a quarter of every slot handed out. Two thirds of the
catalogue is never shown to anyone. Those twenty employers get hundreds of
applications they cannot read; the other 1,800 get nothing. Precision@10 is
excellent throughout.

**Gini and coverage are the metrics. They are cheap, and almost nobody
computes them,** because the dashboard aggregates over users and this failure
only exists across users.

### The twins

Candidates 4 and 5 have near-identical resumes on purpose. Jaccard of their
top-10 lists: **0.54**. Whatever arbitrary tiebreak separates them is stable
across every query, so one of them is permanently second — for every job,
forever.

## The fix that does not work

Penalise jobs that already have a lot of applications:

```
  congestion   Gini    coverage
  0.0          0.860    32.2%
  1.0          0.867    31.2%
  4.0          0.869    30.8%      <- no improvement at any strength
```

The shape is right and the input is wrong. The `interactions` table records
what users did *last month*. The pile-up being measured is what the ranker is
doing *right now*, to users who have not applied to anything yet. The penalty
is reading a number that does not know about the problem.

This is the most useful thing in the scenario: a plausible fix, implemented
correctly, that moves nothing. Keep the measurement, not the intention.

## The fix that works, and what it costs

Feed the penalty from what *this serving round* has already handed out:

```
  feedback   Gini    coverage   top-10 kept
  0.0        0.860    32.2%     100.0%
  1.0        0.608    62.0%      41.5%
  3.0        0.559    66.6%      34.9%
  8.0        0.551    66.6%      33.4%
```

Gini 0.86 → 0.56, coverage 32% → 67%. The price is that only 35% of the
original top 10 survives. **Whether that is worth paying is a product
decision, not a modelling one** — but you cannot have the argument until the
penalty reads the right number.

## The architectural consequence

Serving user 200 now depends on what users 1–199 were shown. The recommender is
no longer a pure function of `(user, catalogue)`:

- the tally is shared mutable state, so it needs a home (Redis, a sliding
  window) and an expiry policy;
- two app servers without a shared tally will both flood the same job;
- the same request twice gives different answers, so caching and testing both
  change shape.

That is not incidental. **A cross-user problem cannot be fixed by a per-user
function**, and the cost of fixing it is statelessness. Every fairness,
diversity or exploration mechanism has this shape.

```bash
./lab.sh run 04
./lab.sh suggest-jobs 4 --congestion 2.0
```
