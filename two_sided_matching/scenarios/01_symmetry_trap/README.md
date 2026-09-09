# 01 — The symmetry trap

**The claim you should be able to defend afterwards:** embedding both sides and
running a nearest-neighbour search is not a two-sided recommender. It is a
one-sided recommender that you can read from either end, and the difference
shows up the first time the two parties disagree.

## The setup everyone starts with

Embed the resume. Embed the job description. Rank by cosine. Mirror the query
for the other direction:

```sql
-- jobs for a candidate
SELECT j.id FROM jobs j JOIN job_docs d ON d.job_id = j.id
ORDER BY d.embedding <=> $resume_vec LIMIT 20;

-- candidates for a job  ("same thing, swap the tables")
SELECT c.id FROM candidates c JOIN resumes r ON r.candidate_id = c.id
ORDER BY r.embedding <=> $job_vec LIMIT 20;
```

That is a real and useful retrieval system. It is not two-sided, and the reason
is one line of arithmetic.

## Why it cannot be two-sided

Cosine similarity is a dot product of unit vectors, and a dot product does not
care which argument you wrote first:

```
cos(resume, jd) = resume · jd = jd · resume = cos(jd, resume)
```

The proof runs it on the real corpus and compares the two matrices elementwise.
The maximum difference is `0.00e+00` — not "small", **identical**.

So there is one score matrix `S`. "Jobs for a candidate" is a row of it.
"Candidates for a job" is a column. Two views, one relation. Nothing you do to
the embedding model changes that, because it is a property of the operator, not
of the vectors.

## What the single number cannot say

The proof searches 1,600 scored pairs for the one the two sides disagree about
most. On the corpus as scraped it finds something like:

```
  reading                 score     what it says
  --------------------------------------------------------------
  cosine (symmetric)      0.166     one number, offered to both parties
  employer wants them     1.000     qualified, would interview
  they want the job       0.141     fails their non-negotiables
  harmonic mean           0.248     pairing should not happen
```

A staff engineer, textually a strong match, who will not take the job. The
employer should see them near the top of their list. They should not see the
job anywhere near the top of theirs. **One number cannot hold both of those
opinions**, and which one you show is decided by whichever side happens to be
looking at the screen.

## The fix, and the one trap inside it

Score each direction separately, then combine with a **harmonic mean**:

```python
s_candidate_to_job = 0.5 * similarity + 0.5 * candidate_wants(cand, job)
s_job_to_candidate = 0.5 * similarity + 0.5 * employer_wants(cand, job)
score = 2ab / (a + b)
```

Harmonic and not arithmetic, because the arithmetic mean lets one side carry
the other:

```
arithmetic(1.0, 0.2) = 0.60      harmonic(1.0, 0.2) = 0.33
arithmetic(0.6, 0.6) = 0.60      harmonic(0.6, 0.6) = 0.60
```

Both pairs average the same. Only the second is a match. The harmonic mean is
dominated by the smaller argument, which is the arithmetic of "both parties
have to say yes".

**The trap:** the same reasoning applies *inside* each direction and it is easy
to miss. This lab shipped it wrong first. `candidate_wants` averaged its
components, so a job paying 110k scored **0.43** for a candidate whose floor is
400k — the pay component was 0.0, and "it is remote" and "it is in your
country" averaged it back up to nearly half marks. The dealbreaker got
averaged away.

The fix is a geometric mean over the components, because multiplying lets one
bad term dominate the way a dealbreaker should:

```
arithmetic(0.0,  0.8, 1.0) = 0.60      <- recommends the job
geometric (0.05, 0.8, 1.0) = 0.37      <- does not
```

Watch for this wherever you combine sub-scores. The mean you reach for by
reflex is the one that cannot express "no".

## What actually changes

```
  candidate   rank corr   top-10 kept   cos #1    two-sided #1
  ------------------------------------------------------------
  1           +0.47       6/10          103       2
  2           +0.61       5/10          992       725
  3           +0.58       7/10          314       314
  4           +0.57       8/10          773       773
```

Rank correlation around **+0.5**, and three or four of every ten top-10 results
replaced. Not decoration — a different ranking.

## The result that surprises people

Take a candidate's #1 job, then ask that job for its top candidates. Under a
symmetric score, surely they come back first?

```
  candidate     their #1 job    their rank at that job
  ------------------------------------------------------
  4             773             4
  20            1111            6
  55            146             2
  300           571             1
```

One in five. And this is with the *symmetric* score, so it is not a modelling
artefact — **the two queries rank different populations.** Your favourite job
is chosen from two thousand jobs; you are chosen from four hundred candidates.
Being someone's top pick is not a property of the pair, it is a property of the
pair *and everyone else who applied*.

That is the real content of the word "two-sided", and it is why scenario 04
(congestion) exists.

## Run it

```bash
./lab.sh run 01
./lab.sh suggest-jobs 4 --scoring cosine --filters off     # the naive system
./lab.sh suggest-jobs 4                                    # the two-sided one
```

Look at the `wants→` and `←wanted` columns. Under `--scoring cosine` they are
always equal. That equality is the whole bug.
