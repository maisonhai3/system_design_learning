# 06 — Cold start

**The claim:** content-based retrieval has one great virtue — it works on day
zero — and one blind spot that no amount of ranking can fix.

## The new posting

Insert a job nobody has seen, clicked, saved or applied to. It has **zero rows
in `interactions`**. Collaborative filtering has literally nothing: the row
does not appear in its input at all. Same for anything popularity-ranked.

**That is why a purely behavioural recommender cannot launch a marketplace,
only grow one.**

Content-based retrieval finds the right candidates immediately — the pinned
data-engineering candidates appear in its top 10 on the day it was posted. The
document *is* the signal.

## A tempting claim, and why it is false

The obvious next thought: a new job has no applicants, so the congestion
penalty from scenario 04 leaves it alone while shrinking crowded incumbents —
fairness machinery doubling as cold-start promotion, two problems for one term.

It is wrong, and the scenario keeps the measurement rather than the intention:

```
  serving round: slots handed out        1990
  load accumulated by the NEW job           5
  load on the busiest incumbent            14

  the new listing's lead widened for 0 of 2 candidates who can see it
```

A new job that matches a narrow niche saturates that niche within a few hundred
requests and then looks exactly like an incumbent. **Congestion control
equalises load; it has no notion of age.** Asking one term to carry both is how
a fairness mechanism quietly becomes a growth hack that does neither job.

The real fix is a separate, explicit term: a decaying boost on
impressions-since-published, or an epsilon of exploration traffic reserved for
under-served listings. Separate because it needs its own decay, its own budget,
and its own defence against the obvious exploit — delete and repost to reset
your age. (Which is why the loader fingerprints posting *text* and counts
reposts rather than trusting the posting id.)

## The other cold start, which is worse

```
  best cosine for a one-line resume     0.328
  best cosine for a full resume         0.449
```

A thin profile lands near the centroid of the corpus: weakly similar to
everything, strongly similar to nothing. Never anyone's top match, always in
everyone's pool.

**There is no modelling fix for missing input.** The fix is a product one — ask
for skills at signup. Which is why onboarding forms exist, and why "just upload
your CV and we'll do the rest" is a ranking decision disguised as a UX one.

```bash
./lab.sh run 06
```
